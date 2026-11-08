# Signed vendor OVN26 database migration fixture

This job exercises real vendor `ovsdb-server`, `ovsdb-tool`, `ovn-nbctl`, `ovn-sbctl` and `ovn-northd` binaries against newly created, private Unix-socket databases. It checks a previous-version schema migration and a restoration from those fixture databases' own verified before-images. It does not connect to an existing OVN installation.

## Exact vendor identities

| Component | RPM package identity | Expected binary identity |
| --- | --- | --- |
| Baseline OVN | `ovn25.09` and `ovn25.09-central`, `25.09.3-100.el9s.x86_64` | `25.09.5-100.el9s`, OVS library `3.6.2` |
| Candidate OVN | `ovn26.03`, `ovn26.03-central`, `ovn26.03-host`, `26.03.3-54.el9s.x86_64` | `26.03.4-54.el9s`, OVS library `3.7.2` |
| Database tools | `openvswitch3.5-3.5.3-6.el9s.x86_64` | `3.5.3-6.el9s` |

The RPM versions and executable banners are distinct vendor identities. The candidate's `26.03.4-54.el9s` banner does not establish an upstream `26.03.4` release tag. Every RPM URL, exact byte count, SHA256, expected metadata, signing-key fingerprint and selected payload path is fixed in [artifacts.lock.json](artifacts.lock.json).

The workflow uses the official AlmaLinux 9.8 amd64 image by immutable manifest digest. Its base supplies Python 3.9, RPM verification tools, OpenSSL 3.5.8, systemd and nghttp2 runtime libraries. Four additional pinned Alma RPMs supply only selected libcap-ng, libevent, libunbound and protobuf-c library files. The selected library closure was inspected from the actual vendor ELF dependencies; it is not inferred from the RPM version labels.

Primary artifact sources are the [CentOS NFV repository](https://mirror.stream.centos.org/SIGs/9-stream/nfv/x86_64/openvswitch-2/Packages/o/), the [CentOS NFV public key](https://www.centos.org/keys/RPM-GPG-KEY-CentOS-SIG-NFV), the [AlmaLinux 9.8 BaseOS repository](https://repo.almalinux.org/almalinux/9.8/BaseOS/x86_64/os/Packages/), and the [AlmaLinux 9 public key](https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux-9). The [AlmaLinux container documentation](https://wiki.almalinux.org/containers/docker-images.html) identifies its official image distribution.

## Preparation and execution boundaries

Preparation runs inside a disposable, read-only container image with a newly created fixture mount. It downloads only the locked public URLs, rejects redirects, bounds input sizes, verifies SHA256 and each primary public-key fingerprint, then checks RPM signatures using a new fixture-local RPM key database. It requires each RPM's exact name/version/release/architecture. It never installs an RPM, runs package hooks, imports keys into the host key database or starts a packaged service.

Only explicitly listed binaries, schemas and library files are extracted. A bounded CPIO reader rejects unexpected selected-member types and confines library symlinks to reviewed sibling files. Extracted file contents and links are compared with the signed RPM headers. Package archives and extracted members are checked again before fixture execution. Certificates, host profiles, production trust material, protected recovery records, original database files and private product source are not included.

The second container has **no network**, a read-only image filesystem, no Linux capabilities, no new privileges, and bounded CPU, memory and process counts. Every DB, Unix socket, control socket, PID, temporary directory and log path is below its new fixture root. The `ovn-controller` artifact is checked with `--version` only; no controller, virtual switch, chassis or guest is started. All synthetic MAC addresses are locally administered and the two IP addresses come from the documentation-only `192.0.2.0/24` range.

## Required evidence

The runner fails unless all of these stages complete:

1. Verify all ten signed package archives, selected payload identities, executable banners and input schema identities.
2. Create fresh baseline NB/SB databases and query their actual schema versions: NB `7.12.0`, SB `21.5.0`.
3. Seed two synthetic ports and an allow-related/drop ACL pair. Run baseline northd and require both expected SB bindings, nonzero logical flows, matching NB/SB configuration counters and zero chassis.
4. Stop only the fixture's owned processes, retain and hash its own database before-images, and perform offline conversion. Query the converted on-disk and running schema versions: NB `7.18.0`, SB `21.8.0`.
5. Require the original NB UUID, address, port-security, group and ACL projection to remain identical. Run candidate northd and require fresh configuration convergence, expected bindings and nonzero flows again.
6. Stop the owned processes and restore the fixture's saved before-image bytes. Check byte identity **before restarting** any database, then query the restored baseline schemas, compare the NB projection and run baseline translation once more.
7. Join every process created by the fixture. A cleanup failure prevents PASS.

Restoration uses saved original bytes; it does not attempt or claim a schema downgrade. Later legitimate database writes after restart are not described as byte-identical. The result JSON distinguishes the restored-byte check from later logical projection and translation checks.

The harness is an adaptation of a prior synthetic migration procedure with all machine names, private paths, host inventory reads, product Git access, installed service/package observations and private adapter tests removed. It is newly scoped public fixture code, not a claim that those removed tests executed here. The independent review and real hosted job determine whether this adaptation qualifies.

## Limits

A passing job establishes this exact vendor artifact pair's behavior in its isolated test environment. It does not install OVN26 on any original host, authorize a recovery operation, resolve an expired review, establish shared-fabric quorum, exercise guest packets, prove tenant isolation, or qualify physical HA, automated renewal or production rollback. No production-readiness flag is set by this fixture.

These upstream/vendor binaries retain their respective upstream licenses; the downloads are not republished as repository files or release artifacts. Source code in this repository retains the existing license notices.
