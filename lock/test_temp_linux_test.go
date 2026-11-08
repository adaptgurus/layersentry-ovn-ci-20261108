//go:build linux

package ovn

import (
	"os"
	"path/filepath"
	"testing"
)

// Several security tests exercise production checks that require a root-owned,
// non-writable installation ancestry. Keep those checks intact and make their
// host-only fixture requirement explicit on unprivileged CI runners.
func rootOwnedTestTempDir(t *testing.T, pattern string) (string, error) {
	t.Helper()
	if os.Geteuid() != 0 {
		t.Skip("requires root-owned fixture ancestry; production ownership checks are not weakened for unprivileged CI")
	}
	base := os.Getenv("TMPDIR")
	if base == "" {
		base = "/root"
	}
	if err := validateDeploymentFileAncestors(filepath.Join(base, "anchor")); err != nil {
		return "", err
	}
	return os.MkdirTemp(base, pattern)
}
