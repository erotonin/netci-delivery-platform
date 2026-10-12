# Immutable artifact Shared Library pipeline

This example is prepared for the `global-library-examples` section of
`jenkinsci/pipeline-examples`. It demonstrates four reusable patterns:

1. build, SBOM, vulnerability scan, sign and publish are explicit stages;
2. signing uses a Jenkins secret-file credential rather than command-line or
   repository material;
3. evidence is archived from its own directory so Jenkins does not walk the
   entire workspace over an agent channel;
4. reusable agents are cleaned while disposable agents rely on pod/workspace
   destruction.

Place `immutableArtifactPipeline.groovy` in the Shared Library's `vars/`
directory. A consuming repository calls:

```groovy
@Library('delivery-library') _

immutableArtifactPipeline(
  agentLabel: 'ephemeral-build',
  disposableAgent: true,
  scriptDirectory: 'scripts/ci',
  signingKeyCredentialId: 'cosign-signing-key-file',
)
```

The repository must provide executable `test.sh`, `build.sh`, `sbom.sh`,
`scan.sh`, `sign.sh` and `publish.sh` scripts. Each script owns one tool-specific
operation; the library owns ordering, credential scope and evidence lifecycle.

Before upstream submission, run the example in a Jenkins test harness and adapt
the README path and license header to the target repository.
