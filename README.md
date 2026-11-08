# Public OVN source and vendor database fixtures

This repository runs the reviewed Linux boot-lock regression subset in a standard GitHub-hosted Ubuntu runner. It is a small, self-contained source fixture derived from source commit `c8841cf0e501b05944e64a2c81de60db02870577`.

A separate [vendor OVN26 fixture](vendor/README.md) verifies exact signed public vendor artifacts and runs real NB/SB migration, northd translation and saved-before-image restoration inside a network-disabled execution container. Its workflow is independent of the boot-lock test counts. Neither workflow installs a product release on an original host or certifies production readiness.

## What the job proves

The seven top-level tests exercise real local kernel file locks and private temporary fixture files:

- Ordinary operations reject contention without waiting.
- Two boot waiters serialize on the same lock inode.
- Cancellation and deadlines interrupt waiting without retaining ownership.
- Cancellation immediately after acquisition closes the acquired descriptor.
- Symlinks, shared permissions and additional hard links are rejected.
- Path replacement and mode drift are rejected.
- Unexpected kernel-lock errors remain distinguishable from contention.

Normal and race runs must each contain all seven top-level tests and fifteen passing test events, with zero failures or skips. Vet covers the isolated package. This is not the complete product source suite, an OVN version upgrade, a running database cluster, tenant traffic testing or production certification.

## Source identity and scope

`SOURCE.json` identifies every origin file, its Git blob and SHA256, and the exact extraction. The lock implementation, lock tests and protected temporary-root helper are copied byte-for-byte. The ancestry validator is the unchanged function from its source file with only the imports/package wrapper needed for this isolated package. Its original lines and function hash are recorded explicitly.

The local module uses only the Go standard library. Its `example.invalid` module name is an inert identifier, not a fetched endpoint. There are no third-party Go modules. The source contains no lab inventory or addresses, credentials, certificate/key material, protected runtime profiles, recovery receipts or private repository history.

The original LayerSentry proprietary license and attribution notices are preserved in `LICENSE` and `NOTICE`. Publication of this fixture does not introduce an additional license grant. The referenced Go authors' license is retained in `GO-LICENSE`.

The public workflow pins checkout and setup-go to full commit identities and selects Go 1.26.8. Its token permissions are read-only, checkout does not persist credentials, and there are no configured secrets or external host connections. Tests run as root only inside the disposable hosted runner so the original ownership/ancestry checks execute without being weakened or skipped. The test subprocess receives a clean environment, disabled Go module download endpoints, and dedicated temporary cache paths. The tests create and remove their own private temporary roots; they do not use installed OVN services or production lock paths.

The Ubuntu runner label pins the operating-system family, not an immutable hosted-runner image. The workflow has an overall ten-minute limit and each test command has a three-minute limit.

## Verification

The workflow verifies `CONTENTS.sha256` before testing. Its event checker requires the exact package, every started test to pass exactly once, and exactly one package-level passing terminal event. It rejects any skip, failed package or incomplete event stream, including a log truncated after the last test but before package completion. Raw Go test events remain visible in the hosted job logs; no deployment artifact is produced.

On a disposable Linux machine with root access and Go 1.26.8:

```sh
sha256sum --check CONTENTS.sha256
go test -json -count=1 -timeout=3m ./lock > normal.jsonl
python3 ci/assert_test_events.py normal.jsonl
go test -race -json -count=1 -timeout=3m ./lock > race.jsonl
python3 ci/assert_test_events.py race.jsonl
go vet ./lock
```

These commands require root to exercise the protected fixture ancestry. A successful public job establishes this source subset's behavior on its hosted runner. Live OVN recovery and production acceptance remain separate work.
