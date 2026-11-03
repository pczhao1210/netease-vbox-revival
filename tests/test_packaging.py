import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class PackagingTests(unittest.TestCase):
    def test_start_script_aliases_and_fixed_tags(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            executable = temporary / "docker"
            log = temporary / "docker.jsonl"
            executable.write_text(
                "#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ['DOCKER_TEST_LOG'],'a') as output: output.write(json.dumps(sys.argv[1:])+'\\n')\nif sys.argv[1:3] == ['version','--format']: print('linux/amd64')\nif sys.argv[1:3] == ['buildx','inspect']: print('Name: fixture\\nPlatforms: ' + os.environ.get('FAKE_PLATFORMS', 'linux/amd64*, linux/arm64'))\n"
            )
            executable.chmod(0o755)
            environment = dict(
                os.environ,
                PATH=str(temporary) + os.pathsep + os.environ.get("PATH", ""),
                DOCKER_TEST_LOG=str(log),
            )
            environment.pop("DOCKERHUB_NAMESPACE", None)
            for alias in ("build", "--build"):
                log.unlink(missing_ok=True)
                subprocess.run(
                    ["bash", str(root / "start.sh"), alias],
                    cwd=temporary,
                    env=environment,
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                build = next(call for call in calls if call[:2] == ["buildx", "build"])
                self.assertEqual(build[build.index("--tag") + 1], "neteast-spk-server:latest")
                self.assertFalse(any(call[0] in ("run", "compose") for call in calls))
                self.assertIn("--load", build)
                self.assertEqual(build[build.index("--platform") + 1], "linux/amd64")
                self.assertNotIn("--push", build)
                self.assertEqual(build[-1], str(root))
                self.assertEqual(build[build.index("--target") + 1], "runtime")
            for args in (("push",), ("--push",), ("build", "--push"), ("--build", "push")):
                log.unlink(missing_ok=True)
                subprocess.run(
                    ["bash", str(root / "start.sh"), *args, "--namespace", "fixture-owner"],
                    cwd=temporary,
                    env=environment,
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                build = next(call for call in calls if call[:2] == ["buildx", "build"])
                self.assertEqual(
                    build[build.index("--tag") + 1],
                    "docker.io/fixture-owner/neteast-spk-server:latest",
                )
                self.assertFalse(any(call[0] in ("run", "compose") for call in calls))
                self.assertEqual(build[build.index("--platform") + 1], "linux/arm64,linux/amd64")
                self.assertIn("--push", build)
                self.assertNotIn("--load", build)
                self.assertEqual(sum(call[:2] == ["buildx", "build"] for call in calls), 1)
            log.unlink(missing_ok=True)
            subprocess.run(
                ["bash", str(root / "start.sh"), "push"],
                cwd=temporary,
                env=environment,
                capture_output=True,
                check=True,
                timeout=5,
            )
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            build = next(call for call in calls if call[:2] == ["buildx", "build"])
            self.assertEqual(
                build[build.index("--tag") + 1], "docker.io/pczhao1210/neteast-spk-server:latest"
            )
            result = subprocess.run(
                ["bash", str(root / "start.sh"), "push"],
                cwd=temporary,
                env=dict(environment, FAKE_PLATFORMS="linux/amd64, linux/386"),
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("tonistiigi/binfmt", result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(call[0] in ("run", "compose") for call in calls))

    def test_start_script_rejects_missing_namespace_and_custom_tag(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment.pop("DOCKERHUB_NAMESPACE", None)
        for args in (
            ("push", "--namespace", ""),
            ("build", "--tag", "different:version"),
            ("push", "--namespace", "other/target"),
        ):
            result = subprocess.run(
                ["bash", str(root / "start.sh"), *args],
                env=environment,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 2)

    def test_workflows_parse_and_run_steps_are_shell_strings(self):
        subprocess.run(
            [
                "node",
                "-e",
                "const fs=require('node:fs'),yaml=require('js-yaml');for(const file of ['.github/workflows/images.yml','.github/workflows/offline.yml']){const doc=yaml.safeLoad(fs.readFileSync(file,'utf8'));for(const job of Object.values(doc.jobs)){for(const step of job.steps||[]){if(step.run!==undefined&&typeof step.run!=='string')throw new Error('run must be a shell string');}}}",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=True,
            timeout=5,
        )

    def test_build_assets_do_not_supply_the_target_node_binary(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text()
        self.assertIn("FROM --platform=$BUILDPLATFORM node:", dockerfile)
        self.assertIn("COPY --from=node-runtime /usr/local/bin/node", dockerfile)
        self.assertNotIn("COPY --from=node-assets /usr/local/bin/node", dockerfile)
        self.assertEqual(dockerfile.count("--only-binary=:all:"), 2)
        self.assertIn("FROM voice AS runtime", dockerfile)

    def test_compose_services_share_the_published_runtime_image(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                "node",
                "-e",
                "const fs=require('node:fs'),yaml=require('js-yaml');process.stdout.write(JSON.stringify(yaml.safeLoad(fs.readFileSync('compose.yaml','utf8'))))",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        services = json.loads(result.stdout)["services"]
        self.assertEqual(
            set(services), {"control", "media", "music-resolver", "voice", "mcp-worker"}
        )
        for service in services.values():
            self.assertEqual(service["image"], "${SPK_IMAGE:-neteast-spk-server:latest}")
            self.assertEqual(service["build"]["target"], "runtime")
            self.assertTrue(service["read_only"])
            self.assertEqual(service["user"], "1000:1000")
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertIn("no-new-privileges:true", service["security_opt"])
            for forbidden in (
                "privileged",
                "devices",
                "device_cgroup_rules",
                "cap_add",
                "pid",
                "network_mode",
            ):
                self.assertNotIn(forbidden, service)
            for volume in service["volumes"]:
                self.assertNotIn("/dev/", volume)
                self.assertNotIn("docker.sock", volume)
        self.assertEqual(services["music-resolver"]["environment"]["HOME"], "/accounts")

    def test_publication_requires_native_runtime_checks(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/images.yml").read_text()
        self.assertIn("runner: ubuntu-24.04-arm", workflow)
        self.assertIn("needs: native-smoke", workflow)
        self.assertIn('[[ "$(uname -m)" == "$TARGET_MACHINE" ]]', workflow)
        self.assertIn("preflight --voice --strict", workflow)
        self.assertIn('--platform "$TARGET_PLATFORM"', workflow)
        self.assertIn("dst=/app/tests,readonly", workflow)
        self.assertIn("dst=/app/device,readonly", workflow)
        self.assertIn("dst=/app/Dockerfile,readonly", workflow)
        self.assertIn("dst=/app/.github,readonly", workflow)
        self.assertIn("dst=/app/start.sh,readonly", workflow)
        self.assertIn("dst=/app/compose.yaml,readonly", workflow)
        self.assertIn("discover -s /app/tests", workflow)
        self.assertNotIn("dst=/app/src", workflow)
        self.assertIn("--entrypoint python spk-smoke:runtime", workflow)
        self.assertIn("--target runtime --load", workflow)

    def test_bake_publishes_the_runtime_image_for_arm64_and_amd64(self):
        root = Path(__file__).resolve().parents[1]
        bake = json.loads((root / "docker-bake.json").read_text())
        self.assertEqual(bake["group"]["default"]["targets"], ["runtime"])
        target = bake["target"]["runtime"]
        self.assertEqual(set(target["platforms"]), {"linux/arm64", "linux/amd64"})
        self.assertEqual(target["target"], "runtime")
        self.assertEqual(target["tags"], ["${IMAGE}"])


if __name__ == "__main__":
    unittest.main()
