// Run `body` as one netCI-visible stage: report `running` first, `succeeded` or `failed`
// after, with timing. The Portal's stage graph is drawn from these reports; before this
// helper a succeeded build showed every stage "Pending" because nothing ever said
// otherwise. Reporting is best effort and never fails the build itself -- a stage that
// built the artifact is not undone by a callback that could not be delivered.
//
//   netciStage('unit-test', 'Unit Test', callbackCredentialsId) { sh 'bash test.sh' }
def call(String stageId, String stageName, String callbackCredentialsId, Closure body) {
    boolean reportable = env.NETCI_PIPELINE_RUN_ID?.trim() && env.NETCI_API_URL?.trim()
    long started = System.currentTimeMillis()
    String startedAt = new Date(started).format("yyyy-MM-dd'T'HH:mm:ss'Z'", TimeZone.getTimeZone('UTC'))
    if (reportable) {
        report(callbackCredentialsId, "stage --id '${stageId}' --name '${stageName}' --status running --started-at '${startedAt}'")
    }
    try {
        body()
    } catch (err) {
        if (reportable) {
            long ms = System.currentTimeMillis() - started
            String completedAt = new Date().format("yyyy-MM-dd'T'HH:mm:ss'Z'", TimeZone.getTimeZone('UTC'))
            String reason = (err.toString() ?: 'failed').replaceAll("'", '')
            report(callbackCredentialsId, "stage --id '${stageId}' --name '${stageName}' --status failed --started-at '${startedAt}' --completed-at '${completedAt}' --duration-ms ${ms} --error '${reason.take(500)}'")
        }
        throw err
    }
    if (reportable) {
        long ms = System.currentTimeMillis() - started
        String completedAt = new Date().format("yyyy-MM-dd'T'HH:mm:ss'Z'", TimeZone.getTimeZone('UTC'))
        report(callbackCredentialsId, "stage --id '${stageId}' --name '${stageName}' --status succeeded --started-at '${startedAt}' --completed-at '${completedAt}' --duration-ms ${ms}")
    }
}

private void report(String callbackCredentialsId, String arguments) {
    try {
        netciInBuilder {
            netciCallbackAuth(callbackCredentialsId) {
                sh "python3 \"\${NETCI_TOOLING_DIR}/scripts/netci_callback.py\" ${arguments} >/dev/null"
            }
        }
    } catch (ignored) {
        echo "netCI stage report skipped: ${ignored}"
    }
}
