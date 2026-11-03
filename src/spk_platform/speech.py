import asyncio
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .network import bounded_json, remote_client


def speech_host(value):
    host = urlsplit(value).hostname or ""
    suffixes = (
        ".speech.microsoft.com",
        ".speech.azure.cn",
        ".cognitiveservices.azure.com",
        ".cognitiveservices.azure.cn",
        ".api.cognitive.microsoft.com",
        ".api.cognitive.azure.cn",
    )
    if not host.endswith(suffixes):
        raise ValueError("Speech SDK endpoints must use an Azure Speech resource domain")


def tts_url(config, voices=False):
    base = config.tts_endpoint
    if not base:
        if not config.region:
            raise ValueError("Azure Speech region or endpoint is required")
        base = "https://%s.tts.speech.microsoft.com" % config.region
    base = base.rstrip("/")
    if base.endswith("/cognitiveservices/v1"):
        base = base[: -len("/cognitiveservices/v1")]
    return base + ("/cognitiveservices/voices/list" if voices else "/cognitiveservices/v1")


def ssml(text, config):
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("TTS text must contain 1 to 4000 characters")
    namespace = "http://www.w3.org/2001/10/synthesis"
    ET.register_namespace("", namespace)
    root = ET.Element(
        "{%s}speak" % namespace,
        {"version": "1.0", "{http://www.w3.org/XML/1998/namespace}lang": config.language},
    )
    voice = ET.SubElement(root, "{%s}voice" % namespace, {"name": config.voice})
    prosody = ET.SubElement(
        voice, "{%s}prosody" % namespace, {"rate": "%+d%%" % round((config.rate - 1) * 100)}
    )
    prosody.text = text
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


async def voices(config, key, private_hosts=()):
    if not key:
        raise ValueError("Azure TTS key is not configured")
    async with remote_client(private_hosts, timeout=15) as client:
        async with client.stream(
            "GET", tts_url(config, voices=True), headers={"Ocp-Apim-Subscription-Key": key}
        ) as response:
            result = await bounded_json(response, 2097152)
    return [
        {"name": item["ShortName"], "locale": item["Locale"], "gender": item.get("Gender", "")}
        for item in result
        if "ShortName" in item and "Locale" in item
    ]


async def tts_chunks(text, config, key, private_hosts=(), client=None):
    if not key:
        raise ValueError("Azure TTS key is not configured")
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-48khz-192kbitrate-mono-mp3",
        "User-Agent": "spkbrain/1",
    }
    owned_client = client is None
    client = client or remote_client(private_hosts, timeout=30)
    try:
        async with asyncio.timeout(60):
            async with client.stream(
                "POST", tts_url(config), content=ssml(text, config), headers=headers
            ) as response:
                response.raise_for_status()
                received = 0
                async for chunk in response.aiter_bytes(16384):
                    received += len(chunk)
                    if received > 16 * 1048576:
                        raise ValueError("TTS output exceeds limit")
                    yield chunk
                if not received:
                    raise ValueError("Azure returned empty audio")
    finally:
        if owned_client:
            await client.aclose()


async def synthesize(text, config, key, path, private_hosts=(), client=None):
    with path.open("wb") as output:
        async for chunk in tts_chunks(text, config, key, private_hosts, client):
            output.write(chunk)


class RecognitionEpoch:
    def __init__(self):
        self.generation = 0
        self.final_ids = set()

    def begin(self):
        self.generation += 1
        self.final_ids.clear()
        return self.generation

    def accept(self, generation, result_id):
        if generation != self.generation or result_id in self.final_ids:
            return False
        self.final_ids.add(result_id)
        return True

    def cancel(self):
        self.generation += 1
        self.final_ids.clear()


class AzureASR:
    def __init__(self, config, key, emit):
        import azure.cognitiveservices.speech as sdk

        if not key or not (config.asr_endpoint or config.region):
            raise ValueError("Azure ASR endpoint/region and key are required")
        if config.asr_endpoint:
            speech_host(config.asr_endpoint)
            speech = sdk.SpeechConfig(subscription=key, endpoint=config.asr_endpoint)
        else:
            speech = sdk.SpeechConfig(subscription=key, region=config.region)
        speech.speech_recognition_language = config.language
        self.sdk = sdk
        self.emit = emit
        self.closed = False
        self.input_ended = False
        self.stream = sdk.audio.PushAudioInputStream(
            stream_format=sdk.audio.AudioStreamFormat(
                samples_per_second=16000, bits_per_sample=16, channels=1
            )
        )
        self.recognizer = sdk.SpeechRecognizer(
            speech_config=speech, audio_config=sdk.audio.AudioConfig(stream=self.stream)
        )
        self.recognizer.recognizing.connect(self._partial)
        self.recognizer.recognized.connect(self._final)
        self.recognizer.canceled.connect(self._cancelled)

    def _partial(self, event):
        if not self.closed:
            self.emit("partial", event.result.text, event.result.result_id)

    def _final(self, event):
        if self.closed:
            return
        kind = (
            "final" if event.result.reason == self.sdk.ResultReason.RecognizedSpeech else "no_match"
        )
        self.emit(kind, event.result.text, event.result.result_id)

    def _cancelled(self, event):
        if not self.closed:
            self.emit("cancelled", "", "")

    async def start(self):
        await asyncio.wait_for(
            asyncio.to_thread(self.recognizer.start_continuous_recognition_async().get), 10
        )

    def write(self, pcm):
        if not self.closed and not self.input_ended:
            self.stream.write(pcm)

    def end_input(self):
        if not self.input_ended:
            self.input_ended = True
            self.stream.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.end_input()
        self.recognizer.recognizing.disconnect_all()
        self.recognizer.recognized.disconnect_all()
        self.recognizer.canceled.disconnect_all()
        await asyncio.wait_for(
            asyncio.to_thread(self.recognizer.stop_continuous_recognition_async().get), 5
        )
