import argparse
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

from .auth import initialize_tokens, set_admin_password, token_file
from .state import State


def preflight(voice=False):
    result = {
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "docker_cli": bool(shutil.which("docker")),
        "cloud_calls": False,
        "device_access": False,
    }
    if result["ffmpeg"]:
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-t",
                "0.1",
                "-f",
                "null",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
    if voice:
        try:
            import azure.cognitiveservices.speech as speech
            import sherpa_onnx
            import webrtcvad

            speech.audio.AudioStreamFormat(samples_per_second=16000, bits_per_sample=16, channels=1)
            result["speech_sdk"] = speech.__version__
            result["sherpa_onnx"] = sherpa_onnx.__version__
            result["vad"] = isinstance(webrtcvad.Vad(2).is_speech(bytes(960), 16000), bool)
        except (ImportError, OSError, RuntimeError) as error:
            result["voice_error"] = type(error).__name__
        model = Path(os.getenv("SPK_KWS_DIR", "/models/kws"))
        if (model / "model.json").is_file():
            from .voice import KeywordWake

            KeywordWake(model)
            result["kws"] = "model-loaded"
        else:
            result["kws"] = "native-wake-only; optional KWS assets not supplied"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="R76s speaker container services")
    parser.add_argument(
        "command",
        choices=[
            "init",
            "serve",
            "preflight",
            "backup",
            "export-device-token",
            "export-bootstrap-token",
            "import-legacy",
            "reset-admin-password",
        ],
    )
    parser.add_argument(
        "service", nargs="?", choices=["control", "media", "resolver", "mcp", "voice"]
    )
    parser.add_argument("--host", default=os.getenv("SPK_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--state-dir", type=Path, default=Path(os.getenv("SPK_STATE_DIR", "data/control"))
    )
    parser.add_argument("--key-dir", type=Path, default=Path(os.getenv("SPK_KEY_DIR", "secrets")))
    parser.add_argument(
        "--media-root", type=Path, default=Path(os.getenv("SPK_MEDIA_ROOT", "data/media"))
    )
    parser.add_argument(
        "--local-root", type=Path, default=Path(os.getenv("SPK_LOCAL_ROOT", "data/local"))
    )
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    if args.command == "reset-admin-password":
        if not (args.state_dir / "control.sqlite3").is_file():
            parser.error("Existing control database required; check --state-dir")
        if not sys.stdin.isatty():
            parser.error(
                "Use an interactive trusted terminal; passwords are not accepted as arguments or piped input"
            )
        state = State(args.state_dir, args.key_dir / "master.key")
        try:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    password = getpass.getpass("New administrator password (12-256 characters): ")
                    confirmation = getpass.getpass("Confirm new administrator password: ")
            except getpass.GetPassWarning:
                print("Cannot hide terminal input; password unchanged.", file=sys.stderr)
                return 2
            except (EOFError, KeyboardInterrupt):
                print("Password reset cancelled; nothing changed.", file=sys.stderr)
                return 2
            if password != confirmation:
                print("Passwords do not match; nothing changed.", file=sys.stderr)
                return 2
            try:
                set_admin_password(state, password)
            except ValueError:
                print(
                    "Password must contain 12 to 256 characters; nothing changed.", file=sys.stderr
                )
                return 2
            print(
                "Administrator password updated. Previous sessions are invalid; configuration, cloud keys and media are unchanged."
            )
            return 0
        finally:
            state.close()
    if args.command == "preflight":
        result = preflight(args.voice)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return int(
            args.strict
            and (not result["ffmpeg"] or not result["ffprobe"] or "voice_error" in result)
        )
    if args.command in ("export-device-token", "export-bootstrap-token"):
        print(
            token_file(
                args.key_dir, "device" if args.command == "export-device-token" else "bootstrap"
            )
        )
        return 0
    if args.command == "init":
        initialize_tokens(args.key_dir)
        state = State(args.state_dir, args.key_dir / "master.key")
        state.close()
        print(
            "Initialized private state and service tokens. No cloud requests or device commands were sent."
        )
        return 0
    if args.command == "import-legacy":
        from .settings import Settings

        for canonical, legacy in (
            ("SPK_LLM_KEY", "DS_KEY"),
            ("SPK_LLM_MODEL", "DS_MODEL"),
            ("SPK_LLM_BASE", "DS_BASE"),
        ):
            if (
                os.getenv(canonical)
                and os.getenv(legacy)
                and os.getenv(canonical) != os.getenv(legacy)
            ):
                raise ValueError(
                    "Conflicting legacy environment variables: " + canonical + " / " + legacy
                )
        config = Settings.from_legacy()
        if "/anthropic" in config.llm.endpoint:
            raise ValueError(
                "Legacy endpoint uses Anthropic Messages; configure a Chat/Responses API base explicitly"
            )
        state = State(args.state_dir, args.key_dir / "master.key")
        try:
            key = os.getenv("SPK_LLM_KEY") or os.getenv("DS_KEY")
            if key:
                state.put_secret(config.llm.key_ref, key)
            print("Imported draft revision", state.save(config, int(state.meta("saved"))))
        finally:
            state.close()
        return 0
    if args.command == "backup":
        state = State(args.state_dir, args.key_dir / "master.key")
        try:
            print(state.backup())
        finally:
            state.close()
        return 0
    if not args.service:
        parser.error("serve requires a service name")
    if args.service == "control":
        from .control import create_control_app

        app = create_control_app(args.state_dir, args.key_dir)
        port = 8080
    elif args.service == "media":
        from .workers import create_media_app

        async def notify(payload):
            import httpx

            try:
                async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
                    await client.post(
                        os.getenv("SPK_CONTROL_URL", "http://control:8080")
                        + "/internal/media/fetched",
                        json=payload,
                        headers={"Authorization": "Bearer " + token_file(args.key_dir, "media")},
                    )
            except httpx.HTTPError:
                return

        app = create_media_app(args.key_dir, args.media_root, args.local_root, notify=notify)
        port = 8899
    elif args.service == "resolver":
        from .resolver import create_resolver_app

        app = create_resolver_app(args.key_dir)
        port = 8082
    elif args.service == "mcp":
        from .workers import create_mcp_app

        app = create_mcp_app(args.key_dir)
        port = 8083
    else:
        from .workers import create_voice_app

        app = create_voice_app(args.key_dir, os.getenv("SPK_CONTROL_URL", "http://control:8080"))
        port = 8084
    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port or port,
        access_log=False,
        proxy_headers=False,
        timeout_keep_alive=5,
        limit_concurrency=100,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
