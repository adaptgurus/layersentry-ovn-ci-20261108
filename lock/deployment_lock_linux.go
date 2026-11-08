//go:build linux

package ovn

import (
	"context"
	"errors"
	"os"
	"syscall"
	"time"
)

var errNativeDeploymentHostBusy = errors.New("another native OVN operation owns this host")

const nativeGuardBootLockWait = 30 * time.Second

// Production callers always supply the fixed host lock. The private path
// parameter permits real kernel-lock tests without touching the host lock.
// Waiting is reserved for the packaged boot guard; ordinary mutations retain
// fail-fast exclusion. Neither path replaces or deletes the lock file.
func lockNativeDeploymentFile(ctx context.Context, path string, waitForBoot bool) (func(), error) {
	if ctx == nil {
		return nil, errors.New("native host lock requires a context")
	}
	if e := ctx.Err(); e != nil {
		return nil, e
	}
	if os.Geteuid() != 0 {
		return nil, errors.New("installation helper requires its scoped root execution identity")
	}
	if waitForBoot {
		// Bound only acquisition, not the subsequent reconciliation. A shorter
		// caller deadline wins; acquiring a lock never refreshes that deadline.
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, nativeGuardBootLockWait)
		defer cancel()
	}
	fd, e := syscall.Open(path, syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0600)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), "ovn-lock")
	st, e := f.Stat()
	if e != nil {
		f.Close()
		return nil, e
	}
	s, ok := st.Sys().(*syscall.Stat_t)
	if !ok || s.Uid != 0 || s.Nlink != 1 || !st.Mode().IsRegular() || st.Mode().Perm()&0077 != 0 {
		f.Close()
		return nil, errors.New("unsafe native lock")
	}
	if e = acquireNativeDeploymentFlock(ctx, fd, waitForBoot); e != nil {
		// Closing also releases a lock acquired immediately before cancellation.
		f.Close()
		return nil, e
	}
	if e = validateNativeDeploymentLockBinding(f, path, st); e != nil {
		f.Close()
		return nil, e
	}
	if e = ctx.Err(); e != nil {
		f.Close()
		return nil, e
	}
	return func() { _ = syscall.Flock(fd, syscall.LOCK_UN); _ = f.Close() }, nil
}

func validateNativeDeploymentLockBinding(f *os.File, path string, before os.FileInfo) error {
	after, e := f.Stat()
	if e != nil {
		return e
	}
	named, e := os.Lstat(path)
	if e != nil {
		return e
	}
	a, aok := after.Sys().(*syscall.Stat_t)
	b, bok := before.Sys().(*syscall.Stat_t)
	if !aok || !bok || !os.SameFile(before, after) || !os.SameFile(after, named) ||
		!named.Mode().IsRegular() || after.Mode() != before.Mode() || a.Uid != 0 ||
		a.Gid != b.Gid || a.Nlink != 1 || after.Mode().Perm()&0077 != 0 {
		return errors.New("native lock custody or path changed during acquisition")
	}
	return nil
}

func acquireNativeDeploymentFlock(ctx context.Context, fd int, waitForBoot bool) error {
	for {
		if e := ctx.Err(); e != nil {
			return e
		}
		e := syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB)
		if e == nil {
			// The caller closes the descriptor on error, so a cancellation at
			// this boundary cannot leave a lock or admit reconciliation.
			return ctx.Err()
		}
		if !errors.Is(e, syscall.EWOULDBLOCK) && !errors.Is(e, syscall.EAGAIN) {
			return e
		}
		if !waitForBoot {
			return errNativeDeploymentHostBusy
		}
		timer := time.NewTimer(50 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}
