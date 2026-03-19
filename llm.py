import json
import re
from time import perf_counter

import requests
from loguru import logger

GLOBAL_LLM = None
DEFAULT_VOLCENGINE_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEFAULT_VOLCENGINE_MODEL = "doubao-seed-2-0-lite-260215"


class LLM:
    def __init__(
        self,
        volcengine_api_key: str = None,
        volcengine_base_url: str = DEFAULT_VOLCENGINE_BASE_URL,
        volcengine_model: str = DEFAULT_VOLCENGINE_MODEL,
    ):
        self.volcengine_api_key = volcengine_api_key
        self.volcengine_base_url = volcengine_base_url
        self.volcengine_model = volcengine_model
        self.enabled = bool(volcengine_api_key)
        if self.enabled:
            logger.info(
                "Bilingual TLDR provider: Volcengine Ark ({})",
                self.volcengine_model,
            )
        else:
            logger.warning(
                "VOLCENGINE_API_KEY is not set. TLDR generation is disabled."
            )

    def _request(self, messages: list[dict], max_tokens: int = 500) -> str:
        if not self.enabled:
            return ""
        started = perf_counter()
        payload = {
            "model": self.volcengine_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.volcengine_api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(
            self.volcengine_base_url,
            headers=headers,
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        result = data["choices"][0]["message"]["content"].strip()
        logger.debug(
            "Generated bilingual TLDR with {} in {:.2f}s",
            self.volcengine_model,
            perf_counter() - started,
        )
        return result

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    @staticmethod
    def _clean_tldr(text: str) -> str:
        cleaned = text.strip()
        cleaned = re.sub(r"^(English TLDR|Chinese TLDR|中文TLDR|中文翻译)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip().strip('"')

    def _parse_bilingual_json(self, text: str) -> dict[str, str]:
        cleaned = self._strip_code_fence(text)
        candidates = [cleaned]
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                en = str(data.get("en") or data.get("english") or "").strip()
                zh = str(data.get("zh") or data.get("chinese") or "").strip()
                if en or zh:
                    return {
                        "en": self._clean_tldr(en),
                        "zh": self._clean_tldr(zh),
                    }
        return {"en": "", "zh": ""}

    def generate_bilingual_tldr(self, paper_prompt: str) -> dict[str, str]:
        if not self.enabled:
            return {"en": "", "zh": ""}
        response = self._request(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You summarize scientific papers. "
                        "Return valid JSON only, with exactly two keys: "
                        '{"en":"...","zh":"..."}. '
                        "Rules: each value must be a single-sentence TLDR; "
                        "`en` must be fluent academic English; "
                        "`zh` must be fluent Simplified Chinese; "
                        "do not output markdown, code fences, explanations, bullet points, labels, or chain-of-thought; "
                        "do not copy the English sentence into `zh`; "
                        "preserve technical terms when needed but keep the Chinese sentence natural."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Generate bilingual TLDRs for the following paper content.\n\n"
                        f"{paper_prompt}\n\n"
                        'Return JSON only in the form {"en":"...","zh":"..."}.'
                    ),
                },
            ],
            max_tokens=500,
        )
        parsed = self._parse_bilingual_json(response)
        if parsed["en"] or parsed["zh"]:
            return parsed
        logger.warning("Failed to parse bilingual TLDR response cleanly. Returning empty TLDRs.")
        return {"en": "", "zh": ""}


def set_global_llm(
    volcengine_api_key: str = None,
    volcengine_base_url: str = DEFAULT_VOLCENGINE_BASE_URL,
    volcengine_model: str = DEFAULT_VOLCENGINE_MODEL,
):
    global GLOBAL_LLM
    GLOBAL_LLM = LLM(
        volcengine_api_key=volcengine_api_key,
        volcengine_base_url=volcengine_base_url,
        volcengine_model=volcengine_model,
    )


def get_llm() -> LLM:
    if GLOBAL_LLM is None:
        logger.info(
            "No global LLM found, creating a default one. Use `set_global_llm` to set a custom one."
        )
        set_global_llm()
    return GLOBAL_LLM
