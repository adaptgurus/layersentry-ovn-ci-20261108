//go:build linux

package ovn

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
)

func validateDeploymentFileAncestors(path string) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return errors.New("invalid installation path")
	}
	for parent := filepath.Dir(path); ; parent = filepath.Dir(parent) {
		st, e := os.Lstat(parent)
		if e != nil {
			return e
		}
		s, ok := st.Sys().(*syscall.Stat_t)
		if !ok || !st.IsDir() || s.Uid != 0 || st.Mode().Perm()&0022 != 0 {
			return errors.New("unsafe installation directory")
		}
		if parent == "/" {
			break
		}
	}
	return nil
}
