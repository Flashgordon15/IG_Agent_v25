// IG Agent macOS-native supervisor — orchestrates agent_kill → agent_start → agent_verify → agent_gui.
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func main() {
	root, err := findProjectRoot()
	if err != nil {
		fmt.Fprintf(os.Stderr, "igagent_launcher: %v\n", err)
		os.Exit(1)
	}

	launcher := filepath.Join(root, "macos", "launcher")
	scripts := []struct {
		name string
		fail bool
	}{
		{"agent_kill.sh", true},
		{"agent_start.sh", true},
		{"agent_verify.sh", true},
		{"agent_gui.sh", false},
	}

	for _, step := range scripts {
		path := filepath.Join(launcher, step.name)
		if _, err := os.Stat(path); err != nil {
			fmt.Fprintf(os.Stderr, "missing script: %s\n", path)
			os.Exit(1)
		}
		fmt.Printf("==> %s\n", step.name)
		cmd := exec.Command("/bin/bash", path)
		cmd.Dir = root
		cmd.Env = append(os.Environ(),
			"IG_AGENT_ROOT="+root,
			"PYTHONPATH="+filepath.Join(root, "src"),
		)
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			if step.fail {
				fmt.Fprintf(os.Stderr, "step failed: %s: %v\n", step.name, err)
				os.Exit(1)
			}
			fmt.Fprintf(os.Stderr, "warn: %s: %v\n", step.name, err)
		}
	}
	fmt.Println("✅ igagent_launcher complete")
}

func findProjectRoot() (string, error) {
	if env := strings.TrimSpace(os.Getenv("IG_AGENT_ROOT")); env != "" {
		if isProjectRoot(env) {
			return filepath.Clean(env), nil
		}
	}

	exe, err := os.Executable()
	if err == nil {
		dir := filepath.Dir(exe)
		for i := 0; i < 8; i++ {
			if isProjectRoot(dir) {
				return dir, nil
			}
			// App bundle: Contents/MacOS/exe → repo is ../../../..
			if strings.HasSuffix(dir, filepath.Join("Contents", "MacOS")) {
				candidate := filepath.Clean(filepath.Join(dir, "..", "..", "..", ".."))
				if isProjectRoot(candidate) {
					return candidate, nil
				}
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}

	cwd, _ := os.Getwd()
	dir := cwd
	for i := 0; i < 8; i++ {
		if isProjectRoot(dir) {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}

	defaults := []string{
		"/Users/chrisgordon/Projects/IG_Agent_v25",
		filepath.Join(os.Getenv("HOME"), "Projects", "IG_Agent_v25"),
	}
	for _, d := range defaults {
		if isProjectRoot(d) {
			return d, nil
		}
	}
	return "", fmt.Errorf("project root not found (set IG_AGENT_ROOT)")
}

func isProjectRoot(dir string) bool {
	if dir == "" {
		return false
	}
	start := filepath.Join(dir, "scripts", "start.sh")
	mainPy := filepath.Join(dir, "src", "main.py")
	_, e1 := os.Stat(start)
	_, e2 := os.Stat(mainPy)
	return e1 == nil && e2 == nil
}
