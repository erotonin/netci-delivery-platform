package homesync

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
)

// A build is taken to be running, or about to, for this long after its number was reserved or its
// running copy last written, unless its finished record is there. A copy is written every 3 s
// (ADR-079), and the copy's deletion and the finished record are two listeners of one event, in
// no set order -- without the margin, a rename could slip between them.
const busyMargin = "2 minutes"

var (
	// ErrNoItem: there is no item at the name a move starts from.
	ErrNoItem = errors.New("there is no item to move")
	// ErrBadMove: a move onto itself, into its own subtree, or of a path that is not an item.
	ErrBadMove = errors.New("an item moves to another name, outside itself")
)

// ExistsError: the name a move goes to is an item already.
type ExistsError struct{ Path string }

func (e *ExistsError) Error() string { return e.Path + " exists already" }

// BusyError: a build under the moved name is running, or has just been given its number, on a
// replica. Jenkins refuses to rename a job while it builds; across replicas it is this store
// that knows.
type BusyError struct {
	Job     string
	Number  int
	Replica string
}

func (e *BusyError) Error() string {
	return fmt.Sprintf("%s #%d is running on %s; an item is not renamed or moved while it builds", e.Job, e.Number, e.Replica)
}

// MoveResult: the destination's new version and content digest, the old name's tombstone version,
// and how many paths moved (items, their children, and their finished builds).
type MoveResult struct {
	Version     int64  `json:"version"`
	SHA256      string `json:"sha256"`
	FromVersion int64  `json:"fromVersion"`
	Moved       int    `json:"moved"`
}

// under: the column is $1 or below it, compared with starts_with (LIKE would read `_` and `%` in a
// job's name as wildcards).
func under(column string) string {
	return "(" + column + " = $1 OR starts_with(" + column + ", $1 || '/'))"
}

// Move renames or moves an item, its children and its finished builds, in one transaction (ADR-087):
//   - the old name, every child and every build of them become tombstones, the old name's
//     marked with where it went (the first entry of the move in the feed);
//   - the same content appears under the new names, parents before children, builds last;
//   - the jobs' leases and reserved numbers go with them, so numbering continues where it was.
//
// It is refused when the item is not there, the destination is, a lock on the item or below it is
// another replica's, or a build under it is running anywhere (BusyError). With dryRun everything is
// checked and nothing written: the plugin asks before Jenkins renames its own copy, which it
// cannot take back. fromToken and toToken are the caller's locks on both names (0: none held).
func (s *Store) Move(ctx context.Context, from, to, origin string, fromToken, toToken int64, dryRun bool) (MoveResult, error) {
	if from == "" || to == "" || origin == "" {
		return MoveResult{}, errors.New("from, to and origin are required")
	}
	if from == to || strings.HasPrefix(to, from+"/") || strings.HasPrefix(from, "@") || strings.HasPrefix(to, "@") {
		return MoveResult{}, ErrBadMove
	}
	tx, err := s.Pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return MoveResult{}, err
	}
	defer tx.Rollback(context.Background()) //nolint:errcheck

	// Both names' rows, locked in a fixed order (two moves crossing each other's names would
	// otherwise each hold one and wait for the other).
	first, second := from, to
	if second < first {
		first, second = second, first
	}
	for _, p := range []string{first, second} {
		if _, err := tx.Exec(ctx, `INSERT INTO home_items (path, version, deleted, updated_by)
			VALUES ($1, 0, true, $2) ON CONFLICT (path) DO NOTHING`, p, origin); err != nil {
			return MoveResult{}, err
		}
	}
	gone := map[string]bool{}
	rows, err := tx.Query(ctx, `SELECT path, deleted FROM home_items WHERE path = ANY($1) ORDER BY path FOR UPDATE`,
		[]string{first, second})
	if err != nil {
		return MoveResult{}, err
	}
	for rows.Next() {
		var p string
		var d bool
		if err := rows.Scan(&p, &d); err != nil {
			rows.Close()
			return MoveResult{}, err
		}
		gone[p] = d
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return MoveResult{}, err
	}
	if gone[from] {
		return MoveResult{}, ErrNoItem
	}
	if !gone[to] {
		return MoveResult{}, &ExistsError{Path: to}
	}
	if err := checkLock(ctx, tx, from, origin, fromToken); err != nil {
		return MoveResult{}, err
	}
	if err := checkLock(ctx, tx, to, origin, toToken); err != nil {
		return MoveResult{}, err
	}
	// A child being edited elsewhere: its edit would be made against a name that no longer is.
	var holder string
	err = tx.QueryRow(ctx, `SELECT holder FROM home_locks WHERE starts_with(path, $1 || '/')
		AND expires_at > now() AND holder <> $2 LIMIT 1`, from, origin).Scan(&holder)
	if err == nil {
		return MoveResult{}, &LockedError{Holder: holder}
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return MoveResult{}, err
	}

	// The leases first, locked: a reservation waits for the move, or the move sees it.
	if _, err := tx.Exec(ctx, `SELECT 1 FROM home_job_owners WHERE `+under("job")+` FOR UPDATE`, from); err != nil {
		return MoveResult{}, err
	}
	var busy BusyError
	err = tx.QueryRow(ctx, `SELECT n.job, n.number, n.owner FROM home_build_numbers n
		WHERE `+under("n.job")+`
		  AND NOT EXISTS (SELECT 1 FROM home_items b WHERE b.path = '@builds/' || n.job || '/' || n.number AND NOT b.deleted)
		  AND (n.reserved_at > now() - interval '`+busyMargin+`'
		       OR EXISTS (SELECT 1 FROM home_items r WHERE r.path = '@running/' || n.job || '/' || n.number
		                  AND (NOT r.deleted OR r.updated_at > now() - interval '`+busyMargin+`')))
		ORDER BY n.job, n.number LIMIT 1`, from).Scan(&busy.Job, &busy.Number, &busy.Replica)
	if err == nil {
		return MoveResult{}, &busy
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return MoveResult{}, err
	}
	// A running copy with no reservation is not made by the plugin, but would be a running build.
	var running string
	err = tx.QueryRow(ctx, `SELECT path FROM home_items WHERE starts_with(path, '@running/' || $1 || '/') AND NOT deleted LIMIT 1`,
		from).Scan(&running)
	if err == nil {
		job, number := splitBuild(strings.TrimPrefix(running, "@running/"))
		return MoveResult{}, &BusyError{Job: job, Number: number, Replica: "a replica"}
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return MoveResult{}, err
	}
	if dryRun {
		return MoveResult{}, nil
	}

	type row struct {
		path, sum string
		content   []byte
	}
	rows, err = tx.Query(ctx, `SELECT path, content, sha256 FROM home_items
		WHERE NOT deleted AND (`+under("path")+` OR starts_with(path, '@builds/' || $1 || '/')) ORDER BY path FOR UPDATE`, from)
	if err != nil {
		return MoveResult{}, err
	}
	var moving []row
	for rows.Next() {
		var r row
		var sum *string
		if err := rows.Scan(&r.path, &r.content, &sum); err != nil {
			rows.Close()
			return MoveResult{}, err
		}
		if sum != nil {
			r.sum = *sum
		}
		moving = append(moving, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return MoveResult{}, err
	}
	// Followers apply in feed order: an item before its children, every item before any build
	// (a build is written into its job's directory, which must be there).
	sort.SliceStable(moving, func(i, j int) bool {
		bi, bj := strings.HasPrefix(moving[i].path, "@"), strings.HasPrefix(moving[j].path, "@")
		if bi != bj {
			return !bi
		}
		di, dj := strings.Count(moving[i].path, "/"), strings.Count(moving[j].path, "/")
		if di != dj {
			return di < dj
		}
		return moving[i].path < moving[j].path
	})
	renamed := func(p string) string {
		if rest, ok := strings.CutPrefix(p, "@builds/"+from+"/"); ok {
			return "@builds/" + to + "/" + rest
		}
		return to + strings.TrimPrefix(p, from)
	}

	// Leases and numbers go with the jobs. Whatever a deleted item once had under the new names
	// is dropped first: those numbers belonged to builds that are gone.
	for _, table := range []string{"home_job_owners", "home_build_numbers"} {
		if _, err := tx.Exec(ctx, `DELETE FROM `+table+` WHERE `+under("job"), to); err != nil {
			return MoveResult{}, err
		}
		if _, err := tx.Exec(ctx, `UPDATE `+table+` SET job = $2 || substr(job, length($1) + 1) WHERE `+under("job"),
			from, to); err != nil {
			return MoveResult{}, err
		}
	}

	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, feedLock); err != nil {
		return MoveResult{}, err
	}
	var seq int64
	write := func(path string, content []byte, sum string, deleted bool, movedTo string) (int64, error) {
		if _, err := tx.Exec(ctx, `INSERT INTO home_items (path, version, deleted, updated_by)
			VALUES ($1, 0, true, $2) ON CONFLICT (path) DO NOTHING`, path, origin); err != nil {
			return 0, err
		}
		var sumArg *string
		if !deleted {
			sumArg = &sum
		}
		var version int64
		if err := tx.QueryRow(ctx, `UPDATE home_items SET version = version + 1, deleted = $2, content = $3, sha256 = $4,
			updated_by = $5, updated_at = now() WHERE path = $1 RETURNING version`, path, deleted, content, sumArg, origin).Scan(&version); err != nil {
			return 0, err
		}
		var moved *string
		if movedTo != "" {
			moved = &movedTo
		}
		if err := tx.QueryRow(ctx, `INSERT INTO home_changes (path, version, deleted, origin, moved_to) VALUES ($1, $2, $3, $4, $5)
			RETURNING seq`, path, version, deleted, origin, moved).Scan(&seq); err != nil {
			return 0, err
		}
		return version, nil
	}
	var result MoveResult
	// The old name's tombstone, saying where it went, is the move's first entry.
	if result.FromVersion, err = write(from, nil, "", true, to); err != nil {
		return MoveResult{}, err
	}
	for _, r := range moving {
		v, err := write(renamed(r.path), r.content, r.sum, false, "")
		if err != nil {
			return MoveResult{}, err
		}
		if r.path == from {
			result.Version, result.SHA256 = v, r.sum
		}
	}
	for _, r := range moving {
		if r.path != from {
			if _, err := write(r.path, nil, "", true, ""); err != nil {
				return MoveResult{}, err
			}
		}
	}
	result.Moved = len(moving)
	if _, err := tx.Exec(ctx, `SELECT pg_notify($1, $2)`, Notify, fmt.Sprint(seq)); err != nil {
		return MoveResult{}, err
	}
	if beforeCommit != nil {
		beforeCommit(from)
	}
	return result, tx.Commit(ctx)
}

// splitBuild: "team/app/12" is team/app #12.
func splitBuild(p string) (string, int) {
	i := strings.LastIndex(p, "/")
	n, _ := strconv.Atoi(p[i+1:])
	return p[:i], n
}
