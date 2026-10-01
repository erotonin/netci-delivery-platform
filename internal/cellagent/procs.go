package cellagent

import (
	"bytes"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
)

// Processes finds and kills the controller's JVM in the pod's shared process namespace.
type Processes struct {
	ProcRoot string // "/proc"; a directory of fake entries in tests
	Match    string // a substring of the controller's command line, e.g. "jenkins.war"
	Self     int
	Signal   func(pid int, sig syscall.Signal) error
}

// NewProcesses returns the real implementation.
func NewProcesses(match string) *Processes {
	return &Processes{ProcRoot: "/proc", Match: match, Self: os.Getpid(), Signal: func(pid int, sig syscall.Signal) error {
		return syscall.Kill(pid, sig)
	}}
}

func (p *Processes) find() []int {
	entries, err := os.ReadDir(p.ProcRoot)
	if err != nil {
		return nil
	}
	var pids []int
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid == p.Self {
			continue
		}
		cmdline, err := os.ReadFile(filepath.Join(p.ProcRoot, e.Name(), "cmdline"))
		if err != nil {
			continue // exited between the listing and the read
		}
		if bytes.Contains(cmdline, []byte(p.Match)) {
			pids = append(pids, pid)
		}
	}
	return pids
}

// JenkinsRunning reports whether a controller process exists.
func (p *Processes) JenkinsRunning() bool { return len(p.find()) > 0 }

// KillJenkins sends SIGKILL to every controller process and returns how many it signalled.
// SIGKILL, not SIGTERM: a controller that has lost its lease must stop writing now, not after
// an orderly shutdown that writes.
func (p *Processes) KillJenkins() int {
	n := 0
	for _, pid := range p.find() {
		if err := p.Signal(pid, syscall.SIGKILL); err == nil {
			n++
		}
	}
	return n
}
