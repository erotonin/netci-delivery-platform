package runqueue

import (
	"bytes"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Config of the queue service. Secrets are not in it: cell credentials are files in mounted
// directories, client tokens are stored as SHA-256 hashes only.
//
//	{"cells":   {"cell-b": {"url": "http://jenkins.cell-b.svc:8080", "credentials": "/etc/netci/cells/cell-b", "budget": 50}},
//	 "routes":  [{"prefix": "payments/", "cell": "cell-b"}],
//	 "clients": {"gitlab": {"tokenSha256": "<hex>", "jobs": ["payments/"]}}}
type Config struct {
	Cells   map[string]CellConfig   `json:"cells"`
	Routes  []Route                 `json:"routes"`
	Clients map[string]ClientConfig `json:"clients"`
}

// CellConfig: where a cell's controller is and the service account netCI uses there (files
// "user" and "token" in Credentials).
type CellConfig struct {
	URL         string `json:"url"`
	Credentials string `json:"credentials"`
	Budget      int    `json:"budget"`
}

// Route: jobs whose full name starts with Prefix belong to Cell. The longest prefix wins.
type Route struct {
	Prefix string `json:"prefix"`
	Cell   string `json:"cell"`
}

// ClientConfig: a caller of the intake API and the jobs it may trigger (prefixes).
type ClientConfig struct {
	TokenSHA256 string   `json:"tokenSha256"`
	Jobs        []string `json:"jobs"`
}

// LoadConfig reads and checks the file; anything doubtful is an error at start.
func LoadConfig(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var c Config
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("queue config %s: %w", path, err)
	}
	return &c, c.Validate()
}

// Validate checks the configuration's consistency.
func (c *Config) Validate() error {
	if len(c.Cells) == 0 {
		return errors.New("no cells: nothing could be dispatched")
	}
	if len(c.Clients) == 0 {
		return errors.New("no clients: nobody could submit a run")
	}
	for name, cell := range c.Cells {
		if !strings.HasPrefix(cell.URL, "http://") && !strings.HasPrefix(cell.URL, "https://") {
			return fmt.Errorf("cell %s: url must be http(s)", name)
		}
		if cell.Budget <= 0 {
			return fmt.Errorf("cell %s: budget must be positive", name)
		}
		if cell.Credentials == "" {
			return fmt.Errorf("cell %s: no credentials directory", name)
		}
	}
	seen := map[string]bool{}
	for _, r := range c.Routes {
		if _, ok := c.Cells[r.Cell]; !ok {
			return fmt.Errorf("route %q names unknown cell %s", r.Prefix, r.Cell)
		}
		if seen[r.Prefix] {
			return fmt.Errorf("prefix %q is routed twice", r.Prefix)
		}
		seen[r.Prefix] = true
	}
	hashes := map[string]string{}
	for name, cl := range c.Clients {
		h, err := hex.DecodeString(cl.TokenSHA256)
		if err != nil || len(h) != sha256.Size {
			return fmt.Errorf("client %s: tokenSha256 must be a SHA-256 in hex", name)
		}
		if other, dup := hashes[cl.TokenSHA256]; dup {
			return fmt.Errorf("clients %s and %s share a token", other, name)
		}
		hashes[cl.TokenSHA256] = name
		if len(cl.Jobs) == 0 {
			return fmt.Errorf("client %s may trigger no job", name)
		}
	}
	return nil
}

// CellFor returns the cell owning job: the longest matching prefix.
func (c *Config) CellFor(job string) (string, bool) {
	best, cell := -1, ""
	for _, r := range c.Routes {
		if strings.HasPrefix(job, r.Prefix) && len(r.Prefix) > best {
			best, cell = len(r.Prefix), r.Cell
		}
	}
	return cell, best >= 0
}

// Client returns the client whose token this is. Every hash is compared, in constant time, so
// the time taken says nothing about which client or how many matched.
func (c *Config) Client(token string) (string, bool) {
	sum := sha256.Sum256([]byte(token))
	found := ""
	names := make([]string, 0, len(c.Clients))
	for name := range c.Clients {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		want, _ := hex.DecodeString(c.Clients[name].TokenSHA256)
		if subtle.ConstantTimeCompare(sum[:], want) == 1 {
			found = name
		}
	}
	return found, found != ""
}

// MayTrigger reports whether client may trigger job.
func (c *Config) MayTrigger(client, job string) bool {
	for _, p := range c.Clients[client].Jobs {
		if strings.HasPrefix(job, p) {
			return true
		}
	}
	return false
}

// Controllers builds a Jenkins client per cell from its credentials directory.
func (c *Config) Controllers(timeout time.Duration) ([]Cell, error) {
	var cells []Cell
	names := make([]string, 0, len(c.Cells))
	for name := range c.Cells {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		cc := c.Cells[name]
		user, err := os.ReadFile(filepath.Join(cc.Credentials, "user"))
		if err != nil {
			return nil, fmt.Errorf("cell %s: %w", name, err)
		}
		token, err := os.ReadFile(filepath.Join(cc.Credentials, "token"))
		if err != nil {
			return nil, fmt.Errorf("cell %s: %w", name, err)
		}
		cells = append(cells, Cell{Name: name, Budget: cc.Budget, Controller: &Jenkins{
			BaseURL: cc.URL, User: strings.TrimSpace(string(user)), Token: strings.TrimSpace(string(token)),
			Client: &http.Client{Timeout: timeout},
		}})
	}
	return cells, nil
}
