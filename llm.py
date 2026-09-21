import json
import re
import time
from time import perf_counter

import requests
from loguru import logger

GLOBAL_LLM = None
DEFAULT_VOLCENGINE_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEFAULT_VOLCENGINE_MODEL = "doubao-seed-2-0-lite-260215"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"


class LLM:
    def __init__(
        self,
        volcengine_api_key: str = None,
        volcengine_base_url: str = DEFAULT_VOLCENGINE_BASE_URL,
        volcengine_model: str = DEFAULT_VOLCENGINE_MODEL,
        deepseek_api_key: str = None,
        deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        deepseek_model: str = DEFAULT_DEEPSEEK_MODEL,
    ):
        self.provider = "DeepSeek" if deepseek_api_key else "Volcengine Ark"
        self.api_key = deepseek_api_key or volcengine_api_key
        self.base_url = deepseek_base_url if deepseek_api_key else volcengine_base_url
        self.model = deepseek_model if deepseek_api_key else volcengine_model
        self.enabled = bool(self.api_key)
        if self.enabled:
            logger.info("Bilingual TLDR provider: {} ({})", self.provider, self.model)
        else:
            logger.warning("No DEEPSEEK_API_KEY or VOLCENGINE_API_KEY set. TLDR generation is disabled.")

    def _request(self, messages: list[dict], max_tokens: int = 500) -> str:
        if not self.enabled:
            return ""
        started = perf_counter()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if self.provider == "DeepSeek":
            payload["thinking"] = {"type": "disabled"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        max_attempts = 5
        retry_delay_seconds = 1
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    self.base_url,
                    headers=headers,
                    json=payload,
                    timeout=120,
                )
                response.raise_for_status()
                data = response.json()
                result = data["choices"][0]["message"]["content"].strip()
                break
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
                logger.warning(
                    "LLM request attempt {}/{} failed after {:.2f}s: {}",
                    attempt,
                    max_attempts,
                    perf_counter() - started,
                    exc,
                )
                if attempt == max_attempts:
                    return ""
                time.sleep(retry_delay_seconds)
        logger.debug(
            "Generated bilingual TLDR with {} in {:.2f}s",
            self.model,
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

    @staticmethod
    def _looks_like_chinese(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text))

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

    def _parse_json_dict(self, text: str) -> dict:
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
                return data
        return {}

    def _is_valid_bilingual_output(self, data: dict[str, str]) -> bool:
        en = data.get("en", "").strip()
        zh = data.get("zh", "").strip()
        if not en or not zh:
            return False
        if not self._looks_like_chinese(zh):
            return False
        return True

    def _build_messages(self, paper_prompt: str, strict: bool = False) -> list[dict]:
        system_prompt = (
            "You summarize scientific papers. "
            "Return valid JSON only, with exactly two keys: "
            '{"en":"...","zh":"..."}. '
            "`en` must be a single-sentence TLDR in fluent academic English. "
            "`zh` must be a coherent paragraph of approximately 150-200 Chinese characters "
            "(excluding punctuation, Latin letters, and digits), preferably around 175 characters. "
            "Use three or four concise sentences in fluent Simplified Chinese, retaining necessary English technical terms. Prioritize the main contribution and omit secondary details to stay within 150-200 Chinese characters. "
            "Summarize the research question, core method, and main findings supported by the supplied text; "
            "include key quantitative results when informative and available. "
            "Preserve important qualifications and distinguish computational results from experimental validation. "
            "Do not invent results, exaggerate claims, or add generic praise to fill the length target. "
            "Summarize rather than translate the abstract sentence by sentence. "
            "do not output markdown, code fences, explanations, bullet points, labels, or chain-of-thought; "
            "do not copy the English sentence into `zh`; "
            "preserve technical terms when needed but keep the Chinese sentence natural."
        )
        if strict:
            system_prompt += (
                " The `zh` field is invalid unless it contains Chinese characters."
            )
        return [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "Generate an English TLDR and a Chinese summary of approximately 150-200 Chinese characters "
                    "for the following paper content.\n\n"
                    f"{paper_prompt}\n\n"
                    'Return JSON only in the form {"en":"...","zh":"..."}.'
                ),
            },
        ]

    def generate_bilingual_tldr(self, paper_prompt: str) -> dict[str, str]:
        if not self.enabled:
            return {"en": "", "zh": ""}
        for strict in (False, True):
            response = self._request(
                messages=self._build_messages(paper_prompt, strict=strict),
                max_tokens=800,
            )
            if not response:
                logger.warning(
                    "Skipping bilingual TLDR generation because the LLM request returned no content."
                )
                return {"en": "", "zh": ""}
            parsed = self._parse_bilingual_json(response)
            if self._is_valid_bilingual_output(parsed):
                return parsed
            logger.warning(
                "LLM bilingual TLDR response did not contain Chinese characters in zh. Retrying with a stricter prompt."
            )
        logger.warning("Failed to parse bilingual TLDR response cleanly. Returning empty TLDRs.")
        return {"en": "", "zh": ""}

    def extract_affiliations(self, author_prompt: str) -> list[str]:
        if not self.enabled:
            return []
        response = self._request(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract author affiliations from scientific paper author blocks. "
                        'Return valid JSON only in the form {"affiliations":["..."]}. '
                        "Each item must be a concise top-level institution name. "
                        "Prefer corresponding-author affiliations when they are explicitly identifiable; "
                        "otherwise return affiliations in author order. "
                        "Do not include departments, street addresses, postal codes, emails, superscripts, or duplicate institutions. "
                        "Do not output explanations or markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Extract affiliations from the following author information block.\n\n"
                        f"{author_prompt}\n\n"
                        'Return JSON only in the form {"affiliations":["..."]}.'
                    ),
                },
            ],
            max_tokens=300,
        )
        data = self._parse_json_dict(response)
        raw_affiliations = data.get("affiliations", [])
        if not isinstance(raw_affiliations, list):
            return []
        affiliations = []
        seen = set()
        for value in raw_affiliations:
            affiliation = re.sub(r"\s+", " ", str(value).strip()).strip('"')
            if not affiliation:
                continue
            key = affiliation.casefold()
            if key in seen:
                continue
            seen.add(key)
            affiliations.append(affiliation)
        return affiliations


def set_global_llm(
    volcengine_api_key: str = None,
    volcengine_base_url: str = DEFAULT_VOLCENGINE_BASE_URL,
    volcengine_model: str = DEFAULT_VOLCENGINE_MODEL,
    deepseek_api_key: str = None,
    deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
    deepseek_model: str = DEFAULT_DEEPSEEK_MODEL,
):
    global GLOBAL_LLM
    GLOBAL_LLM = LLM(
        volcengine_api_key=volcengine_api_key,
        volcengine_base_url=volcengine_base_url,
        volcengine_model=volcengine_model,
        deepseek_api_key=deepseek_api_key,
        deepseek_base_url=deepseek_base_url,
        deepseek_model=deepseek_model,
    )


def get_llm() -> LLM:
    if GLOBAL_LLM is None:
        logger.info(
            "No global LLM found, creating a default one. Use `set_global_llm` to set a custom one."
        )
        set_global_llm()
    return GLOBAL_LLM
