#!/usr/bin/env python3
"""Real vendor NB/SB migration and before-image restoration on new Unix DBs."""

import argparse
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import time

from prepare import digest, require, verify_selected


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        if len(value) == 2 and value[0] in ("set", "map"):
            return [value[0], sorted((canonical(item) for item in value[1]), key=lambda item: json.dumps(item, sort_keys=True))]
        return [canonical(item) for item in value]
    return value


class Fixture:
    def __init__(self, artifacts, root, lock):
        require(root.is_absolute() and not root.exists() and not root.is_symlink(), "fixture root must be new")
        require(len(str(root).encode()) < 70, "fixture root is too long for private Unix sockets")
        require(artifacts.resolve() != root.resolve() and artifacts.is_dir(), "invalid artifact root")
        self.artifacts, self.root, self.lock = artifacts, root, lock
        self.deadline = time.monotonic() + 180
        self.children, self.logs, self.dbs = [], [], {}
        self.serial = 0
        root.mkdir(mode=0o700)
        for name in ("run", "tmp", "logs"):
            (root / name).mkdir(mode=0o700)
        self.env = {"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LC_ALL": "C",
                    "TMPDIR": str(root / "tmp"), "OVS_RUNDIR": str(root / "run"),
                    "OVN_RUNDIR": str(root / "run"), "OVS_LOGDIR": str(root / "logs"),
                    "OVN_LOGDIR": str(root / "logs"),
                    "LD_LIBRARY_PATH": str(artifacts / "ovs/usr/lib64") + ":" + str(artifacts / "runtime/usr/lib64")}
        self.result = {"scope": "NEW_UNIX_DATABASES_VENDOR_MIGRATION_AND_BEFORE_IMAGE_RESTORE",
                       "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "package_installation": False, "service_actions": False,
                       "chassis_or_guest_packets_tested": False, "production_ready": False,
                       "fixture_pass": False, "all_owned_processes_stopped": False, "stages": []}

    def remaining(self, requested):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "fixture deadline exceeded")
        return min(requested, remaining)

    def run(self, args, timeout=12):
        process = subprocess.run([str(arg) for arg in args], capture_output=True, text=True,
                                 cwd=self.root, env=self.env, timeout=self.remaining(timeout))
        require(process.returncode == 0, "fixture command failed: " + pathlib.Path(str(args[0])).name
                + ": " + process.stderr[-1200:])
        return process.stdout

    def binary(self, role, name):
        return self.artifacts / role / "usr/bin" / name

    def record(self, name, **fields):
        self.result["stages"].append({"stage": name, **fields})
        print(json.dumps({"stage": name, **fields}), flush=True)

    def verify_artifacts(self):
        prior = json.loads((self.artifacts / "verified-artifacts.json").read_text())
        require(len(prior["packages"]) == len(self.lock["packages"]), "incomplete artifact preparation")
        reports = {item["filename"]: item for item in prior["packages"]}
        require(len(reports) == len(prior["packages"]), "duplicate artifact preparation")
        for item in self.lock["packages"]:
            rpm = self.artifacts / "downloads" / item["filename"]
            require(rpm.is_file() and rpm.stat().st_size == item["bytes"]
                    and digest(rpm.read_bytes()) == item["sha256"], "prepared RPM differs from lock")
            actual = verify_selected(item, rpm, self.artifacts / item["root"])
            require(item["filename"] in reports and actual == reports[item["filename"]]["files"],
                    "prepared payload differs from signed artifact")
        versions = {}
        for role in ("baseline", "candidate"):
            names = ["ovn-appctl", "ovn-nbctl", "ovn-sbctl", "ovn-northd"]
            if role == "candidate":
                names.append("ovn-controller")
            for name in names:
                output = self.run([self.binary(role, name), "--version"])
                require(output.splitlines()[0] == name + " " + self.lock["expected"][role]["binary_version"],
                        "unexpected vendor binary banner")
                require("Open vSwitch Library " + self.lock["expected"][role]["ovs_library"] in output.splitlines(),
                        "unexpected vendor OVS library banner")
                versions[role + "/" + name] = output.strip()
            for db, name in (("nb", "OVN_Northbound"), ("sb", "OVN_Southbound")):
                schema = json.loads(self.schema(role, db).read_text())
                require(schema["name"] == name and schema["version"] == self.lock["expected"][role][db + "_schema"],
                        "unexpected vendor schema file")
        for name, path in (("ovsdb-tool", self.binary("ovs", "ovsdb-tool")),
                           ("ovsdb-server", self.artifacts / "ovs/usr/sbin/ovsdb-server")):
            output = self.run([path, "--version"])
            require(output.splitlines()[0] == name + " (Open vSwitch) " + self.lock["expected"]["database_server_version"],
                    "unexpected OVS database tool version")
            versions[name] = output.strip()
        self.record("VERIFIED_ARTIFACTS_AND_BANNERS", versions=versions, package_count=len(reports))

    def schema(self, role, db):
        return self.artifacts / role / "usr/share/ovn" / ("ovn-" + db + ".ovsschema")

    def launch(self, name, args):
        self.remaining(1)
        self.serial += 1
        logpath = self.root / "logs" / (str(self.serial) + "-" + name + ".log")
        log = logpath.open("xb")
        self.logs.append(log)
        process = subprocess.Popen([str(arg) for arg in args], cwd=self.root, env=self.env,
                                   stdout=log, stderr=log, start_new_session=True)
        self.children.append((name, process, logpath))
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        require(process.poll() is not None, "owned child did not stop")

    def clear_owned_endpoint(self, path):
        if path.exists() or path.is_symlink():
            require(not path.is_symlink() and (path.is_socket() or path.is_file()), "unexpected fixture endpoint")
            path.unlink()

    def start_databases(self):
        require(not self.dbs, "existing fixture database processes must be joined first")
        for db in ("nb", "sb"):
            for suffix in ("sock", "ctl", "pid"):
                self.clear_owned_endpoint(self.root / (db + "." + suffix))
            process = self.launch(db, [self.artifacts / "ovs/usr/sbin/ovsdb-server", "--no-chdir",
                                     "--remote=punix:" + str(self.root / (db + ".sock")),
                                     "--unixctl=" + str(self.root / (db + ".ctl")),
                                     "--pidfile=" + str(self.root / (db + ".pid")), "-vsyslog:off",
                                     self.root / (db + ".db")])
            self.dbs[db] = process
            end = time.monotonic() + self.remaining(6)
            while not (self.root / (db + ".sock")).is_socket():
                require(process.poll() is None, "owned database exited before its socket became ready")
                require(time.monotonic() < end, "owned database socket deadline exceeded")
                time.sleep(0.05)
            require(process.poll() is None, "owned database exited during socket readiness")

    def stop_databases(self):
        for process in self.dbs.values():
            self.stop(process)
        self.dbs.clear()

    def versions(self, expected_role):
        result = {}
        for db, name in (("nb", "OVN_Northbound"), ("sb", "OVN_Southbound")):
            require(self.dbs[db].poll() is None, "owned database exited")
            schema = json.loads(self.run([self.binary("ovs", "ovsdb-client"), "get-schema",
                                          "unix:" + str(self.root / (db + ".sock")), name]))
            require(schema["name"] == name and schema["version"] == self.lock["expected"][expected_role][db + "_schema"],
                    "actual database schema differs from expected stage")
            result[db] = schema["version"]
        return result

    def ctl(self, role, db, *args, timeout=12):
        return self.run([self.binary(role, "ovn-" + db + "ctl"), "--timeout=8",
                         "--db=unix:" + str(self.root / (db + ".sock")), *args], timeout)

    def table(self, role, db, name, columns):
        raw = json.loads(self.ctl(role, db, "--format=json", "--columns=" + ",".join(columns), "list", name))
        rows = [canonical(dict(zip(raw["headings"], row))) for row in raw["data"]]
        return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))

    def projection(self, role):
        return {name: self.table(role, "nb", name, columns) for name, columns in {
            "Logical_Switch": ["_uuid", "name", "ports", "external_ids"],
            "Logical_Switch_Port": ["_uuid", "name", "addresses", "port_security", "external_ids"],
            "Port_Group": ["_uuid", "name", "ports", "acls", "external_ids"],
            "ACL": ["_uuid", "direction", "priority", "match", "action", "external_ids"],
        }.items()}

    def northd(self, name, role):
        return self.launch(name, [self.binary(role, "ovn-northd"), "--no-chdir",
                                 "--ovnnb-db=unix:" + str(self.root / "nb.sock"),
                                 "--ovnsb-db=unix:" + str(self.root / "sb.sock"),
                                 "--unixctl=" + str(self.root / (name + ".ctl")),
                                 "--pidfile=" + str(self.root / (name + ".pid")),
                                 "--n-threads=1", "-vsyslog:off"])

    def convergence(self, role, process):
        self.ctl(role, "nb", "--wait=sb", "sync", timeout=12)
        nb = int(self.ctl(role, "nb", "get", "NB_Global", ".", "nb_cfg").strip())
        sb = int(self.ctl(role, "nb", "get", "NB_Global", ".", "sb_cfg").strip())
        names = {row["logical_port"] for row in self.table(role, "sb", "Port_Binding", ["logical_port"])}
        flows = self.table(role, "sb", "Logical_Flow", ["_uuid"])
        chassis = self.table(role, "sb", "Chassis", ["name"])
        require(nb == sb and {"fixture_port_a", "fixture_port_b"} <= names and len(flows) > 0,
                "northd did not produce expected bindings and logical flows with configuration convergence")
        require(not chassis and process.poll() is None, "unexpected chassis or exited northd")
        return {"nb_cfg": nb, "sb_cfg": sb, "logical_ports": sorted(names),
                "logical_flow_count": len(flows), "chassis_count": 0}

    def execute(self):
        self.verify_artifacts()
        tool = self.binary("ovs", "ovsdb-tool")
        for db in ("nb", "sb"):
            self.run([tool, "create", self.root / (db + ".db"), self.schema("baseline", db)])
        self.start_databases()
        initial_versions = self.versions("baseline")
        self.ctl("baseline", "nb", "init")
        self.ctl("baseline", "sb", "init")
        self.ctl("baseline", "nb", "ls-add", "fixture_net")
        for name, mac, address in (("fixture_port_a", "02:00:00:00:01:01", "192.0.2.11"),
                                   ("fixture_port_b", "02:00:00:00:01:02", "192.0.2.12")):
            self.ctl("baseline", "nb", "lsp-add", "fixture_net", name)
            self.ctl("baseline", "nb", "lsp-set-addresses", name, mac + " " + address)
            self.ctl("baseline", "nb", "lsp-set-port-security", name, mac + " " + address)
            self.ctl("baseline", "nb", "set", "Logical_Switch_Port", name, "external_ids:qualification=public-fixture")
        self.ctl("baseline", "nb", "pg-add", "fixture_group", "fixture_port_a", "fixture_port_b")
        self.ctl("baseline", "nb", "acl-add", "fixture_group", "to-lport", "2000", "ip4 && tcp.dst == 443", "allow-related")
        self.ctl("baseline", "nb", "acl-add", "fixture_group", "to-lport", "1000", "ip", "drop")
        old = self.northd("northd-baseline", "baseline")
        self.record("BASELINE_TRANSLATION", schemas=initial_versions, **self.convergence("baseline", old))
        self.stop(old)
        projection = self.projection("baseline")
        projection_hash = digest(json.dumps(projection, sort_keys=True).encode())
        require(len(projection["Logical_Switch_Port"]) == 2 and len(projection["ACL"]) == 2,
                "synthetic port and ACL preimage was not populated")
        self.stop_databases()
        before = {}
        for db in ("nb", "sb"):
            backup = self.root / (db + ".before.db")
            shutil.copyfile(self.root / (db + ".db"), backup)
            before[db] = digest(backup.read_bytes())
            self.run([tool, "convert", self.root / (db + ".db"), self.schema("candidate", db)])
            actual = self.run([tool, "db-version", self.root / (db + ".db")]).strip()
            require(actual == self.lock["expected"]["candidate"][db + "_schema"], "offline conversion schema mismatch")
        self.start_databases()
        migrated_versions = self.versions("candidate")
        require(self.projection("candidate") == projection, "NB UUID/address/ACL projection changed during conversion")
        new = self.northd("northd-candidate", "candidate")
        self.record("CANDIDATE_TRANSLATION", schemas=migrated_versions, **self.convergence("candidate", new))
        self.stop(new)
        require(self.projection("candidate") == projection, "NB projection changed during candidate translation")
        self.stop_databases()
        for db in ("nb", "sb"):
            backup = self.root / (db + ".before.db")
            require(digest(backup.read_bytes()) == before[db], "retained fixture before-image changed")
            shutil.copyfile(backup, self.root / (db + ".db"))
            require(digest((self.root / (db + ".db")).read_bytes()) == before[db], "restoration is not byte-identical")
            actual = self.run([tool, "db-version", self.root / (db + ".db")]).strip()
            require(actual == self.lock["expected"]["baseline"][db + "_schema"], "restored on-disk schema mismatch")
        self.record("BEFORE_IMAGE_BYTES_RESTORED", before_image_sha256=before,
                    byte_identity_checked_before_restart=True, nb_projection_sha256=projection_hash)
        self.start_databases()
        restored_versions = self.versions("baseline")
        require(self.projection("baseline") == projection, "NB projection changed after before-image restore")
        restored = self.northd("northd-restored", "baseline")
        self.record("RESTORED_BASELINE_TRANSLATION", schemas=restored_versions,
                    **self.convergence("baseline", restored))
        self.stop(restored)
        require(self.projection("baseline") == projection, "NB projection changed during restored translation")
        self.stop_databases()
        self.result["fixture_pass"] = True

    def cleanup(self):
        errors = []
        for name, process, _ in reversed(self.children):
            try:
                self.stop(process)
            except Exception as error:
                errors.append(name + ": " + type(error).__name__)
        for log in self.logs:
            log.close()
        self.result["all_owned_processes_stopped"] = not errors and all(process.poll() is not None for _, process, _ in self.children)
        self.result["cleanup_errors"] = errors
        if not self.result["all_owned_processes_stopped"]:
            self.result["fixture_pass"] = False
        self.result["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (self.root / "result.json").write_text(json.dumps(self.result, indent=2) + "\n")
        print(json.dumps(self.result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    fixture = Fixture(pathlib.Path(args.artifacts), pathlib.Path(args.root), json.loads(pathlib.Path(args.lock).read_text()))
    try:
        fixture.execute()
    except Exception as error:
        fixture.result["error"] = type(error).__name__ + ": " + str(error)
    finally:
        fixture.cleanup()
    if not fixture.result["fixture_pass"] or not fixture.result["all_owned_processes_stopped"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
