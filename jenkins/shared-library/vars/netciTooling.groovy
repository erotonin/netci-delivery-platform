// Write netCI's CI tooling -- the callback client and every template's CI scripts --
// from this library onto the agent, and return where it went.
//
// It used to be read out of the checked-out repository, which only held it because every
// lab module built from the netCI repository itself. A real application repository has
// none of it, so a real build failed at its first callback. The library now carries its
// own copy (resources/netci/tooling, kept identical to templates/ and scripts/ by
// scripts/sync_shared_library.py --check), which also pins the tooling to the library
// version the job asked for rather than to whatever the application's commit contains.
//
// The directory is WORKSPACE_TMP, beside the workspace rather than in it: out of the
// Docker build context, so it cannot be copied into an image, and out of reach of the
// checkout, which empties the workspace before extracting the commit. It is written
// before checkout, so a checkout that fails can still be reported.
def call() {
    def root = "${env.WORKSPACE_TMP ?: env.WORKSPACE + '@tmp'}/netci-tooling"
    if (env.NETCI_TOOLING_DIR == root) {
        return root
    }
    def manifest = libraryResource('netci/tooling/MANIFEST')
    dir(root) {
        for (line in manifest.readLines()) {
            def path = line.trim()
            if (path) {
                writeFile file: path, text: libraryResource("netci/tooling/${path}")
            }
        }
    }
    env.NETCI_TOOLING_DIR = root
    return root
}
