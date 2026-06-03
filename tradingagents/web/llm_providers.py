"""多 LLM Provider 抽象層 + 設定管理（支援 API 與 CLI 雙模式）."""
import json
import os
import shutil
import subprocess
from pathlib import Path

SETTINGS_FILE = Path(__file__).parent / "settings.json"

DEFAULT_SETTINGS = {
    "api_keys": {
        "anthropic": "",
        "openai": "",
        "google": "",
    },
    "roles": {
        "smc_structure_analyst": {
            "provider": "openai",
            "model": "gpt-5.5",
            "mode": "cli",
        },
        "analyst": {
            "provider": "openai",
            "model": "gpt-5.5",       # ChatGPT Pro 旗艦推理
            "mode": "cli",
        },
        "reviewer": {
            "provider": "anthropic",
            "model": "opus",          # Claude Opus 4.7 跨家審查
            "mode": "cli",
        },
    },
    "obsidian_vault_path": "",        # 留空則不存 Obsidian 筆記
    # ─── 券商手續費（依個券商最新公告為準；用戶可在 UI 切換 preset） ───
    "brokerage_fees": {
        "tw_broker": "default_60_discount",  # preset 名稱
        "tw_buy_fee_rate": 0.001425 * 0.6,   # 0.1425% × 6 折 = 0.0855%
        "tw_sell_fee_rate": 0.001425 * 0.6,
        "tw_sell_tax_rate_stock": 0.003,     # 股票證交稅 0.3%
        "tw_sell_tax_rate_etf": 0.001,       # ETF 證交稅 0.1%
        "tw_min_fee": 20,                    # 單筆最低手續費 NT$
        # 美股預設 0.25%（依個券商最新公告為準；可在 UI 切換 preset）
        "us_broker": "discount_0_25",
        "us_fee_rate": 0.0025,               # 0.25%
        "us_min_fee": 0,                     # 無最低
        "us_sec_fee_rate": 0.0000278,        # SEC 規費（賣出）2025 年率
    },
}

# 常見券商 preset，UI 可一鍵切換
BROKERAGE_PRESETS = {
    "default_60_discount": {  # 一般網路下單 6 折
        "label": "台股 6 折券商",
        "tw_buy_fee_rate": 0.001425 * 0.6,
        "tw_sell_fee_rate": 0.001425 * 0.6,
        "tw_sell_tax_rate_stock": 0.003,
        "tw_sell_tax_rate_etf": 0.001,
        "tw_min_fee": 20,
    },
    "discount_28": {  # 大量交易 2.8 折
        "label": "台股 2.8 折券商",
        "tw_buy_fee_rate": 0.001425 * 0.28,
        "tw_sell_fee_rate": 0.001425 * 0.28,
        "tw_sell_tax_rate_stock": 0.003,
        "tw_sell_tax_rate_etf": 0.001,
        "tw_min_fee": 20,
    },
    "full_rate": {  # 未打折
        "label": "台股公定費率",
        "tw_buy_fee_rate": 0.001425,
        "tw_sell_fee_rate": 0.001425,
        "tw_sell_tax_rate_stock": 0.003,
        "tw_sell_tax_rate_etf": 0.001,
        "tw_min_fee": 20,
    },
    "discount_0_25": {
        "label": "美股 0.25%（折扣複委託）",
        "us_fee_rate": 0.0025,
        "us_min_fee": 0,
        "us_sec_fee_rate": 0.0000278,
    },
    "fubon_proxy": {
        "label": "富邦複委託 0.5%/min $39.9",
        "us_fee_rate": 0.005,
        "us_min_fee": 39.9,
        "us_sec_fee_rate": 0.0000278,
    },
    "ibkr_tiered": {
        "label": "IBKR Tiered",
        "us_fee_rate": 0.00035,  # 約 0.035%
        "us_min_fee": 0.35,
        "us_sec_fee_rate": 0.0000278,
    },
    "firstrade": {
        "label": "第一證券 Firstrade",
        "us_fee_rate": 0.0,
        "us_min_fee": 0.0,
        "us_sec_fee_rate": 0.0000278,
    },
}

# 各 provider 的 CLI 工具
CLI_TOOLS = {
    "anthropic": "claude",
    "openai": "codex",
    "google": "gemini",
}

# 各 provider 支援的模型清單（CLI 與 API 共用）
AVAILABLE_MODELS = {
    "anthropic": [
        "opus",                       # 別名：最新 Opus（Claude Pro 訂閱可用）
        "sonnet",                     # 別名：最新 Sonnet
        "haiku",                      # 別名：最新 Haiku
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ],
    "openai": [
        "gpt-5.5",                    # 旗艦（Codex CLI 訂閱可用）
        "gpt-5.4",
        "gpt-5.3-codex",
        "gpt-5.4-mini",
        "gpt-5.2",
    ],
    "google": [
        "gemini-3.5-flash",                  # 旗艦推薦（近 Pro 智能、超快超省）
        "gemini-3.1-pro",                    # 高階推理首選
        "gemini-3.1-flash-lite",             # 極致低延遲
        "auto",                              # 自動路由（Gemini CLI 推薦）
        "pro",                               # 別名：當前 pro
        "flash",                             # 別名：當前 flash
        "gemini-3.1-pro-preview",            # 旗艦預覽（Gemini Advanced 訂閱）
        "gemini-3.1-flash-lite-preview",
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
}

WORKFLOW_ROLE_LABELS = {
    "smc_structure_analyst": "SMC 結構分析師",
    "analyst": "分析師",
    "reviewer": "審查員",
}


def build_smc_structure_analyst_prompt(context: str) -> str:
    return f"""你是 SMC 結構分析師，只專注於 Smart Money Concepts / ICT 結構，不討論基本面，也不要泛泛而談。請使用**繁體中文**，根據我提供的上下文中 SMC 區塊、17D 技術矩陣、即時價格與回測摘要，輸出一份可供其他代理人引用的結構化判讀。

{context}

請直接用 markdown 編號條列輸出：

1. **結構偏向** — 當前多空方向、HTF/MTF/LTF 是否對齊、偏向成立的理由
2. **POI 與流動性** — 關鍵 OB / FVG / BOS / CHoCH / DOL / Premium-Discount 區
3. **觸發與失效** — 什麼條件才算可進場，什麼條件代表結構失效
4. **回測與可信度** — 若上下文有回測樣本，說明其支持或限制；若資料不足要明講
5. **一句話結論** — 給綜合分析師的執行摘要
"""


def build_analyst_prompt(context: str, smc_report: str = "") -> str:
    smc_section = f"\n[SMC 結構分析師判讀]\n{smc_report}\n" if smc_report else ""
    return f"""你是專業金融分析師。我提供你這檔標的的歷史財報與基本面、17D 全景技術矩陣（含歷史偏向）、SMC 結構與回測摘要、即時技術指標與大盤環境。請**交叉判讀**後給出操作建議與投資建議。用**繁體中文**。

{context}
{smc_section}

**方法**：把財報（營收/EPS/毛利趨勢）、估值（本益比/成長/賣方目標價）、17D 技術（共振區、各維度偏向、歷史偏向變化）、SMC（結構方向、DOL、回測樣本）四者交叉比對，重點在四者是同向強化還是背離；背離時明確指出並權衡。點位要有依據（進場=技術共振區或 SMC POI 且估值合理；停損=結構失效；停利=目標價/壓力/DOL 共振區）。資料缺口降權。

直接給結論（markdown，條列用編號）：

1. **交叉判讀** — 財報 × 估值 × 17D 技術 × SMC 四者的關係與綜合研判（核心）
2. **操作建議** — 買/賣/持有 + 有依據的進場/停損/停利
3. **投資建議** — 短/中/長線的部位與策略
4. **風險與失效條件**
"""


def build_reviewer_prompt(context: str, analyst_text: str, smc_report: str = "") -> str:
    smc_section = f"\n[SMC 結構分析師判讀]\n{smc_report}\n" if smc_report else ""
    return f"""你是嚴格的投資審查員，負責**找出分析師報告的盲點與弱點**。

[原始數據]
{context}
{smc_section}

[分析師報告]
{analyst_text}

請以**繁體中文** markdown 格式給出**犀利但建設性**的審查意見，不超過 400 字：

## 一、分析師說對的地方
（簡述 1~2 點）

## 二、我有疑慮的地方
（指出邏輯漏洞、忽略的風險、過度樂觀/悲觀）

## 三、我認為錯誤或缺失的部分
（具體指出）

## 四、修正後的建議
（給出你認為更穩健的操作版本）
"""


def workflow_role_sequence(mode: str = "both") -> list[str]:
    if mode == "analyst":
        return ["smc_structure_analyst", "analyst"]
    if mode == "both":
        return ["smc_structure_analyst", "analyst", "reviewer"]
    return []


def build_workflow_prompt(role: str, context: str, prior_outputs: dict | None = None) -> str:
    prior_outputs = prior_outputs or {}
    smc_report = prior_outputs.get("smc_structure_analyst", "")
    analyst_text = prior_outputs.get("analyst", "")
    if role == "smc_structure_analyst":
        return build_smc_structure_analyst_prompt(context)
    if role == "analyst":
        return build_analyst_prompt(context, smc_report=smc_report)
    if role == "reviewer":
        return build_reviewer_prompt(context, analyst_text, smc_report=smc_report)
    raise ValueError(f"Unknown workflow role: {role}")


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            # merge with defaults to handle missing keys
            merged = json.loads(json.dumps(DEFAULT_SETTINGS))
            for k, v in data.items():
                if isinstance(v, dict) and k in merged:
                    merged[k].update(v)
                else:
                    merged[k] = v
            return merged
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    SETTINGS_FILE.chmod(0o600)  # 限制檔案權限保護 API key


def mask_key(key: str) -> str:
    """API key 顯示用 mask: sk-xxx...yyy"""
    if not key:
        return ""
    if len(key) < 12:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


def detect_cli_availability() -> dict:
    """偵測哪些 CLI 工具已安裝且可用。"""
    out = {}
    for provider, cmd in CLI_TOOLS.items():
        path = shutil.which(cmd)
        out[provider] = {
            "cli": cmd,
            "available": bool(path),
            "path": path or "",
        }
    return out


def _find_cli(cmd: str) -> str:
    """找出 CLI 真實路徑（避免 PATH 缺失問題）。"""
    path = shutil.which(cmd)
    if path:
        return path
    # 常見路徑備援
    for p in [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.npm-global/bin")
    ]:
        candidate = os.path.join(p, cmd)
        if os.path.isfile(candidate):
            return candidate
    return cmd


def call_cli(provider: str, model: str, prompt: str, timeout: int = 180) -> str:
    """使用 CLI 工具呼叫 LLM（走訂閱配額，不收 API 費）。"""
    tool = CLI_TOOLS.get(provider)
    if not tool:
        raise ValueError(f"{provider} 沒有對應的 CLI 工具")

    cli_path = _find_cli(tool)
    # 確保 PATH 包含 homebrew 與使用者個人 bin 目錄
    env = os.environ.copy()
    local_bin = os.path.expanduser("~/.local/bin")
    npm_bin = os.path.expanduser("~/.npm-global/bin")
    env["PATH"] = f"{local_bin}:{npm_bin}:/opt/homebrew/bin:/usr/local/bin:{env.get('PATH','')}"

    if provider == "anthropic":
        # Claude Code: claude -p "prompt" --model <model>
        cmd = [cli_path, "-p", prompt, "--model", model]
    elif provider == "openai":
        # Codex CLI: codex exec --skip-git-repo-check --model <model> "prompt"
        cmd = [cli_path, "exec", "--skip-git-repo-check", "--model", model, prompt]
    elif provider == "google":
        # Gemini CLI: gemini -p "prompt" -m <model>
        cmd = [cli_path, "-p", prompt, "-m", model]
    else:
        raise ValueError(f"未知 provider: {provider}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            err = result.stderr or result.stdout or "unknown error"
            raise RuntimeError(f"CLI 執行失敗: {err.strip()[:300]}")
        out = result.stdout
        if not out.strip():
            raise RuntimeError("CLI 沒有輸出（可能未登入：請先在 terminal 執行 " + tool + " 登入）")
        return _clean_cli_output(provider, out)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"CLI 執行超時 ({timeout}s)")


def _clean_cli_output(provider: str, raw: str) -> str:
    """各 CLI 工具的原始輸出清理：去掉 metadata、headers、分隔線等。"""
    lines = raw.splitlines()

    if provider == "openai":
        # Codex 輸出格式：
        #   --------
        #   user
        #   <prompt>
        #   codex
        #   <actual response>
        #   tokens used
        #   <actual response again>
        # 取最後一個 'codex' 標記到 'tokens used' 之間的內容
        out_lines = []
        in_codex_block = False
        for line in lines:
            stripped = line.strip()
            if stripped == "codex":
                in_codex_block = True
                out_lines = []  # 重置取最後一段
                continue
            if stripped.startswith("tokens used") or stripped.startswith("---"):
                in_codex_block = False
                continue
            if in_codex_block:
                out_lines.append(line)
        cleaned = "\n".join(out_lines).strip()
        return cleaned or raw.strip()

    if provider == "google":
        # Gemini CLI 開頭可能有 Warning/Ripgrep/scandir 訊息，過濾掉
        skip_prefixes = ("Warning:", "Ripgrep is not available", "Loaded cached", "MCP STDERR")
        cleaned = "\n".join(l for l in lines if not l.startswith(skip_prefixes))
        return cleaned.strip()

    if provider == "anthropic":
        # Claude Code -p 模式輸出乾淨，直接 strip
        return raw.strip()

    return raw.strip()


def call_llm(provider: str, model: str, prompt: str, api_key: str = "", mode: str = "api", timeout: int = 180) -> str:
    """統一 LLM 呼叫介面。mode='api' 走 SDK 計費 / mode='cli' 走訂閱免費。"""
    if mode == "cli":
        return call_cli(provider, model, prompt, timeout=timeout)
    # api mode
    if not api_key:
        # fallback to env
        env_var = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google": "GOOGLE_API_KEY",
        }.get(provider)
        api_key = os.getenv(env_var, "") if env_var else ""

    if not api_key:
        raise ValueError(f"{provider} API key 未設定")

    if provider == "anthropic":
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        # gpt-5.x / o3 / o4 系列支援 reasoning_effort
        try:
            if model.startswith(("o3", "o4", "gpt-5")):
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    reasoning_effort="medium",
                )
            else:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2000,
                )
        except Exception:
            # 舊版 API 不認 reasoning_effort，降級
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
        return resp.choices[0].message.content

    elif provider == "google":
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        return resp.text

    else:
        raise ValueError(f"不支援的 provider: {provider}")


def run_workflow(context: str, mode: str = "both") -> dict:
    """執行 LLM 工作流。

    mode:
      - 'analyst': 只跑分析師
      - 'reviewer': 只跑審查員（需要 analysis_text 輸入）
      - 'both': 分析師 → 審查員，回傳完整流程
    """
    settings = load_settings()
    keys = settings["api_keys"]
    roles = settings["roles"]

    result = {"context": context, "mode": mode, "steps": []}
    prior_outputs: dict[str, str] = {}

    for role in workflow_role_sequence(mode):
        cfg = roles.get(role, {})
        provider = cfg.get("provider", "")
        model = cfg.get("model", "")
        role_mode = cfg.get("mode", "api")
        prompt = build_workflow_prompt(role, context, prior_outputs)
        try:
            output = call_llm(
                provider,
                model,
                prompt,
                keys.get(provider, ""),
                mode=role_mode,
            )
            prior_outputs[role] = output
            result["steps"].append({
                "role": role,
                "label": WORKFLOW_ROLE_LABELS.get(role, role),
                "provider": provider,
                "model": model,
                "mode": role_mode,
                "output": output,
            })
        except Exception as e:
            result["steps"].append({
                "role": role,
                "label": WORKFLOW_ROLE_LABELS.get(role, role),
                "provider": provider,
                "model": model,
                "mode": role_mode,
                "error": str(e),
            })
            if role in ("analyst", "reviewer"):
                return result

    return result
