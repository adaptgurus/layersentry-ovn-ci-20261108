//go:build linux

package ovn

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"
)

func nativeLockFixture(t *testing.T) string {
	t.Helper()
	dir, e := rootOwnedTestTempDir(t, "ovn-boot-lock-")
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return filepath.Join(dir, "lock")
}

func TestNativeDeploymentOrdinaryLockRemainsFailFast(t *testing.T) {
	path := nativeLockFixture(t)
	unlock, e := lockNativeDeploymentFile(context.Background(), path, false)
	if e != nil {
		t.Fatal(e)
	}
	defer unlock()
	before, e := os.Stat(path)
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	second, e := lockNativeDeploymentFile(ctx, path, false)
	if second != nil {
		second()
		t.Fatal("ordinary operation acquired an already owned lock")
	}
	if !errors.Is(e, errNativeDeploymentHostBusy) || ctx.Err() != nil {
		t.Fatalf("ordinary operation must fail busy before its deadline: %v", e)
	}
	after, e := os.Stat(path)
	if e != nil || !os.SameFile(before, after) {
		t.Fatalf("contention replaced the lock inode: %v", e)
	}
}

func TestNativeGuardBootLocksSerializeTwoWaiters(t *testing.T) {
	path := nativeLockFixture(t)
	holder, e := lockNativeDeploymentFile(context.Background(), path, false)
	if e != nil {
		t.Fatal(e)
	}
	var releaseHolder sync.Once
	defer releaseHolder.Do(holder)
	before, e := os.Stat(path)
	if e != nil {
		t.Fatal(e)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	type acquired struct {
		id int
		e  error
	}
	ready := make(chan struct{}, 2)
	entered := make(chan acquired, 2)
	done := make(chan struct{}, 2)
	release := []chan struct{}{make(chan struct{}), make(chan struct{})}
	for i := range release {
		go func(id int) {
			defer func() { done <- struct{}{} }()
			ready <- struct{}{}
			unlock, e := lockNativeDeploymentFile(ctx, path, true)
			entered <- acquired{id, e}
			if e != nil {
				return
			}
			defer unlock()
			select {
			case <-release[id]:
			case <-ctx.Done():
			}
		}(i)
	}
	defer func() {
		cancel()
		for range release {
			<-done
		}
	}()
	<-ready
	<-ready
	// Neither waiter may fail busy or enter while the original owner remains.
	select {
	case got := <-entered:
		t.Fatalf("boot guard did not wait behind the original owner: %+v", got)
	case <-time.After(150 * time.Millisecond):
	}
	releaseHolder.Do(holder)
	var first acquired
	select {
	case first = <-entered:
		if first.e != nil {
			t.Fatal(first.e)
		}
	case <-ctx.Done():
		t.Fatal("no boot waiter acquired the released lock")
	}
	// An ordinary mutation cannot enter either boot guard's critical section.
	if unlock, e := lockNativeDeploymentFile(ctx, path, false); unlock != nil || !errors.Is(e, errNativeDeploymentHostBusy) {
		if unlock != nil {
			unlock()
		}
		t.Fatalf("boot waiter lost exclusive ownership: %v", e)
	}
	select {
	case got := <-entered:
		t.Fatalf("second boot guard overlapped the first: %+v", got)
	case <-time.After(150 * time.Millisecond):
	}
	close(release[first.id])
	select {
	case second := <-entered:
		if second.e != nil || second.id == first.id {
			t.Fatalf("second guard did not acquire after release: %+v", second)
		}
		close(release[second.id])
	case <-ctx.Done():
		t.Fatal("second guard never acquired the lock")
	}
	after, e := os.Stat(path)
	if e != nil || !os.SameFile(before, after) {
		t.Fatalf("boot serialization replaced or removed the lock: %v", e)
	}
}

func TestNativeGuardBootLockCancellationAndDeadline(t *testing.T) {
	for _, kind := range []string{"cancel-before", "cancel-during", "deadline"} {
		t.Run(kind, func(t *testing.T) {
			path := nativeLockFixture(t)
			holder, e := lockNativeDeploymentFile(context.Background(), path, false)
			if e != nil {
				t.Fatal(e)
			}
			var release sync.Once
			defer release.Do(holder)
			var ctx context.Context
			var cancel context.CancelFunc
			if kind == "deadline" {
				ctx, cancel = context.WithTimeout(context.Background(), 100*time.Millisecond)
			} else {
				ctx, cancel = context.WithCancel(context.Background())
			}
			defer cancel()
			want := context.DeadlineExceeded
			if kind == "cancel-before" {
				cancel()
				want = context.Canceled
			} else if kind == "cancel-during" {
				// The owner remains held throughout: cancellation interrupts a
				// contended acquisition and must not be treated as contention.
				timer := time.AfterFunc(25*time.Millisecond, cancel)
				defer timer.Stop()
				want = context.Canceled
			}
			unlock, e := lockNativeDeploymentFile(ctx, path, true)
			if unlock != nil {
				unlock()
				t.Fatal("expired/canceled waiter received ownership")
			}
			if !errors.Is(e, want) {
				t.Fatalf("got %v, want %v", e, want)
			}
			release.Do(holder)
			fresh, e := lockNativeDeploymentFile(context.Background(), path, false)
			if e != nil {
				t.Fatalf("wait failure retained a lock: %v", e)
			}
			fresh()
		})
	}
}

// A separate kernel lock observer cancels exactly when it sees ownership,
// without depending on the helper's number of context checks or scheduling.
type cancelAtAcquisitionContext struct {
	context.Context
	probeFD int
	cancel  context.CancelFunc
}

func (c *cancelAtAcquisitionContext) Err() error {
	if e := c.Context.Err(); e != nil {
		return e
	}
	e := syscall.Flock(c.probeFD, syscall.LOCK_EX|syscall.LOCK_NB)
	if e == nil {
		return syscall.Flock(c.probeFD, syscall.LOCK_UN)
	}
	if errors.Is(e, syscall.EWOULDBLOCK) || errors.Is(e, syscall.EAGAIN) {
		c.cancel()
		return c.Context.Err()
	}
	return e
}

func TestNativeDeploymentLockCancellationAfterAcquisitionClosesFD(t *testing.T) {
	path := nativeLockFixture(t)
	probe, e := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		t.Fatal(e)
	}
	defer probe.Close()
	parent, cancel := context.WithCancel(context.Background())
	defer cancel()
	ctx := &cancelAtAcquisitionContext{Context: parent, probeFD: int(probe.Fd()), cancel: cancel}
	unlock, e := lockNativeDeploymentFile(ctx, path, false)
	if unlock != nil {
		unlock()
		t.Fatal("canceled acquisition returned an owner")
	}
	if !errors.Is(e, context.Canceled) {
		t.Fatalf("post-acquisition cancellation lost: %v", e)
	}
	fresh, e := lockNativeDeploymentFile(context.Background(), path, false)
	if e != nil {
		t.Fatalf("canceled acquired descriptor was not closed: %v", e)
	}
	fresh()
}

func TestNativeDeploymentLockRejectsUnsafeCustody(t *testing.T) {
	for _, kind := range []string{"symlink", "shared-mode", "hardlink"} {
		t.Run(kind, func(t *testing.T) {
			path := nativeLockFixture(t)
			if e := os.WriteFile(path, nil, 0600); e != nil {
				t.Fatal(e)
			}
			switch kind {
			case "symlink":
				if e := os.Rename(path, path+"-target"); e != nil {
					t.Fatal(e)
				}
				if e := os.Symlink(path+"-target", path); e != nil {
					t.Fatal(e)
				}
			case "shared-mode":
				if e := os.Chmod(path, 0660); e != nil {
					t.Fatal(e)
				}
			case "hardlink":
				if e := os.Link(path, path+"-alias"); e != nil {
					t.Fatal(e)
				}
			}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			unlock, e := lockNativeDeploymentFile(ctx, path, true)
			if unlock != nil {
				unlock()
				t.Fatal("unsafe lock admitted")
			}
			if e == nil || errors.Is(e, errNativeDeploymentHostBusy) || ctx.Err() != nil {
				t.Fatalf("unsafe custody must reject without waiting: %v", e)
			}
		})
	}
}

func TestNativeDeploymentLockRejectsPathAndModeDrift(t *testing.T) {
	for _, kind := range []string{"replacement", "mode"} {
		t.Run(kind, func(t *testing.T) {
			path := nativeLockFixture(t)
			f, e := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
			if e != nil {
				t.Fatal(e)
			}
			defer f.Close()
			before, e := f.Stat()
			if e != nil {
				t.Fatal(e)
			}
			if kind == "replacement" {
				if e := os.Rename(path, path+"-old"); e != nil {
					t.Fatal(e)
				}
				if e := os.WriteFile(path, nil, 0600); e != nil {
					t.Fatal(e)
				}
			} else if e := os.Chmod(path, 0400); e != nil {
				t.Fatal(e)
			}
			if e := validateNativeDeploymentLockBinding(f, path, before); e == nil {
				t.Fatal("changed lock custody or path admitted")
			}
		})
	}
}

func TestNativeDeploymentLockPreservesUnexpectedFlockError(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	e := acquireNativeDeploymentFlock(ctx, -1, true)
	if !errors.Is(e, syscall.EBADF) || ctx.Err() != nil {
		t.Fatalf("unexpected flock error was retried or hidden: %v", e)
	}
}
