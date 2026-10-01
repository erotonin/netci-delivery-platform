# The netCI binaries, built on the host (`make images`), with nothing else in the image: no
# shell, no package manager, nothing to patch. All run as a numeric non-root user.
FROM scratch
COPY bin/linux-amd64/cell-agent /cell-agent
COPY bin/linux-amd64/supervisor /supervisor
COPY bin/linux-amd64/netci-queue /netci-queue
COPY bin/linux-amd64/netci-fabric /netci-fabric
COPY bin/linux-amd64/netci-sandbox /netci-sandbox
USER 65532:65532
ENTRYPOINT ["/supervisor"]
