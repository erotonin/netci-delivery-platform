package fabric

import "time"

// skewOn sets the fabric process's clock d ahead of the database's. Nothing the reconciler decides
// reads it: in production it takes the database's time (Reconciler.Now nil), as here.
func skewOn(w *world, d time.Duration) {
	w.clk.SetTime(w.clk.Now().Add(d))
	w.recon.Now = nil
}
