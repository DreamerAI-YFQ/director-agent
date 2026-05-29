"""
AI编导Agent - 模型Provider抽象层
统一 Claude / Gemini 等多模型的调用接口，支持：
- 统一 analyze() 方法
- 统一重试 + 指数退避 + 超时
- 成本追踪 + 实时告警
- 错误码抽象
"""

import json
import os
import time
import logging
from abc import ABC, abstractmethod
from typing import Optional, Any, Callable
from dataclasses import dataclass, field
from pathlib import Path
from functools import wraps

logger = logging.getLogger(__name__)


# ========== 统一重试工具 ==========

RETRYABLE_PATTERNS = [
    "429", "rate_limit", "rate limit", "overloaded",
    "500", "502", "503", "504", "529",
    "quota", "throttl", "timeout", "timed out",
    "connection reset", "connection refused", "service unavailable",
]


def with_retry(max_retries: int = 3, base_delay: float = 2.0,
               max_delay: float = 60.0, timeout: float = 120.0,
               retryable_patterns: list = None):
    """
    统一重试装饰器

    策略：
    - 指数退避: delay = min(base_delay * 2^attempt, max_delay)
    - 仅对可重试错误重试（429/5xx/限流/超时/连接错误）
    - 不可重试的错误直接抛出（4xx非429、认证错误等）
    - 支持超时参数

    Args:
        max_retries: 最大重试次数（不含首次调用）
        base_delay: 基础等待秒数
        max_delay: 最大等待秒数
        timeout: 单次调用超时（秒）
        retryable_patterns: 自定义可重试错误特征
    """
    patterns = retryable_patterns or RETRYABLE_PATTERNS

    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = str(e)
                    err_lower = last_error.lower()

                    # 判断是否可重试
                    is_retryable = any(p in err_lower for p in patterns)

                    if not is_retryable or attempt >= max_retries:
                        if attempt > 0:
                            logger.error(f"[Retry] {func.__name__} 失败({attempt+1}次): {last_error}")
                        raise

                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logger.warning(
                        f"[Retry] {func.__name__} 第{attempt+1}次重试, 等待{delay:.1f}s: "
                        f"{last_error[:120]}"
                    )
                    time.sleep(delay)

            # 不应到达这里，但兜底
            raise RuntimeError(f"重试耗尽: {last_error}")

        return wrapper
    return decorator


# ========== 成本追踪 ==========

class BudgetExceededError(Exception):
    """预算超限异常（阻止后续API调用）"""
    def __init__(self, message: str, current_cost: float, threshold: float):
        self.current_cost = current_cost
        self.threshold = threshold
        super().__init__(message)


@dataclass
class CostRecord:
    """单次调用成本记录"""
    provider: str           # claude / gemini
    model: str              # 模型名
    stage: str              # P0-P5 或 skill名
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    success: bool = True
    error: str = ""
    timestamp: float = field(default_factory=time.time)


class CostTracker:
    """成本追踪器 - 累计追踪所有模型调用成本"""

    # 模型定价（USD per 1K tokens）
    PRICING = {
        "claude-haiku-4-5": {"input": 0.0008, "output": 0.004},
        "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
        "claude-opus-4": {"input": 0.015, "output": 0.075},
        "gemini-2.5-flash": {"input": 0.00015, "output": 0.0006},
        "gemini-2.5-pro": {"input": 0.00125, "output": 0.005},
        "qwen3-vl-flash": {"input": 0.0001, "output": 0.0003},   # 百炼 VL-Flash 定价
    }

    # 成本告警阈值（USD）
    DEFAULT_THRESHOLDS = {
        "per_run": 0.50,        # 单次Pipeline运行
        "per_hour": 5.00,       # 每小时
        "per_day": 20.00,       # 每天
    }

    def __init__(self, thresholds: dict = None, block_on_exceed: bool = True):
        self.records: list[CostRecord] = []
        self.thresholds = thresholds or self.DEFAULT_THRESHOLDS
        self._alerts: list[dict] = []
        self.block_on_exceed = block_on_exceed  # 超限时是否阻止调用

    def record(self, rec: CostRecord):
        """记录一次调用"""
        self.records.append(rec)
        self._check_thresholds(rec)

    def _check_thresholds(self, rec: CostRecord):
        """检查成本是否超过阈值"""
        now = time.time()

        # 单次Pipeline运行成本（同stage的累计）
        stage_cost = sum(r.cost_usd for r in self.records if r.stage == rec.stage)
        if stage_cost > self.thresholds["per_run"]:
            self._alerts.append({
                "level": "warning",
                "type": "per_run",
                "stage": rec.stage,
                "cost": stage_cost,
                "threshold": self.thresholds["per_run"],
                "message": f"Pipeline阶段 {rec.stage} 累计成本 ${stage_cost:.4f} 超过阈值 ${self.thresholds['per_run']:.2f}",
            })

        # 每小时成本
        hour_ago = now - 3600
        hourly_cost = sum(r.cost_usd for r in self.records if r.timestamp > hour_ago)
        if hourly_cost > self.thresholds["per_hour"]:
            self._alerts.append({
                "level": "critical",
                "type": "per_hour",
                "cost": hourly_cost,
                "threshold": self.thresholds["per_hour"],
                "message": f"每小时成本 ${hourly_cost:.4f} 超过阈值 ${self.thresholds['per_hour']:.2f}",
            })

        # 每天成本
        day_ago = now - 86400
        daily_cost = sum(r.cost_usd for r in self.records if r.timestamp > day_ago)
        if daily_cost > self.thresholds["per_day"]:
            self._alerts.append({
                "level": "critical",
                "type": "per_day",
                "cost": daily_cost,
                "threshold": self.thresholds["per_day"],
                "message": f"每日成本 ${daily_cost:.4f} 超过阈值 ${self.thresholds['per_day']:.2f}",
            })

    def get_alerts(self, clear: bool = True) -> list[dict]:
        """获取并清除告警"""
        alerts = self._alerts.copy()
        if clear:
            self._alerts.clear()
        return alerts

    def check_budget(self, estimated_cost: float = 0.0) -> dict:
        """
        预算检查（调用API前使用，从DB读取保证持久化）

        返回:
            {allowed: bool, level: str, message: str, details: dict}

        如果 block_on_exceed=True 且超限，抛出 BudgetExceededError
        """
        from agent import storage
        try:
            conn = storage.get_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(SUM(cost_usd),0) FROM cost_records
                    WHERE created_at > NOW() - INTERVAL '1 hour'
                """)
                hourly = float(cur.fetchone()[0])

                if hourly > self.thresholds["per_hour"]:
                    msg = f"每小时API成本 ${hourly:.2f} 超过上限 ${self.thresholds['per_hour']:.2f}，已阻止后续调用"
                    if self.block_on_exceed:
                        raise BudgetExceededError(msg, hourly, self.thresholds["per_hour"])
                    return {"allowed": False, "level": "critical", "type": "per_hour",
                            "message": msg, "details": {"current": hourly, "threshold": self.thresholds["per_hour"]}}

                cur.execute("""
                    SELECT COALESCE(SUM(cost_usd),0) FROM cost_records
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                """)
                daily = float(cur.fetchone()[0])

                if daily > self.thresholds["per_day"]:
                    msg = f"每日API成本 ${daily:.2f} 超过上限 ${self.thresholds['per_day']:.2f}，已阻止后续调用"
                    if self.block_on_exceed:
                        raise BudgetExceededError(msg, daily, self.thresholds["per_day"])
                    return {"allowed": False, "level": "critical", "type": "per_day",
                            "message": msg, "details": {"current": daily, "threshold": self.thresholds["per_day"]}}

                return {"allowed": True, "level": "ok", "message": "预算正常"}
        except BudgetExceededError:
            raise
        except Exception as e:
            # DB不可用时回退到内存
            logger.warning(f"[CostTracker] DB检查失败，使用内存数据: {e}")
            now = time.time()
            hourly = sum(r.cost_usd for r in self.records if r.timestamp > now - 3600)
            if hourly > self.thresholds["per_hour"]:
                if self.block_on_exceed:
                    raise BudgetExceededError(f"每小时成本超限", hourly, self.thresholds["per_hour"])
                return {"allowed": False, "level": "critical"}
            return {"allowed": True, "level": "ok", "message": "预算正常(内存)"}

    def get_budget_status(self) -> dict:
        """获取当前预算状态（从DB读取，保证重启不丢失）"""
        from agent import storage
        try:
            conn = storage.get_connection()
            with conn.cursor() as cur:
                # 过去1小时累计
                cur.execute("""
                    SELECT COALESCE(SUM(cost_usd),0), COUNT(*)
                    FROM cost_records
                    WHERE created_at > NOW() - INTERVAL '1 hour'
                """)
                hourly_cost, hourly_calls = cur.fetchone()
                hourly_cost = float(hourly_cost)

                # 过去24小时累计
                cur.execute("""
                    SELECT COALESCE(SUM(cost_usd),0), COUNT(*)
                    FROM cost_records
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                """)
                daily_cost, total_calls = cur.fetchone()
                daily_cost = float(daily_cost)

            return {
                "hourly_cost": round(hourly_cost, 4),
                "hourly_limit": self.thresholds["per_hour"],
                "hourly_pct": round(hourly_cost / self.thresholds["per_hour"] * 100, 1),
                "daily_cost": round(daily_cost, 4),
                "daily_limit": self.thresholds["per_day"],
                "daily_pct": round(daily_cost / self.thresholds["per_day"] * 100, 1),
                "total_calls": total_calls,
            }
        except Exception as e:
            # DB不可用时回退到内存
            logger.warning(f"[CostTracker] DB读取失败，使用内存数据: {e}")
            now = time.time()
            hourly = sum(r.cost_usd for r in self.records if r.timestamp > now - 3600)
            daily = sum(r.cost_usd for r in self.records if r.timestamp > now - 86400)
            return {
                "hourly_cost": round(hourly, 4),
                "hourly_limit": self.thresholds["per_hour"],
                "hourly_pct": round(hourly / self.thresholds["per_hour"] * 100, 1),
                "daily_cost": round(daily, 4),
                "daily_limit": self.thresholds["per_day"],
                "daily_pct": round(daily / self.thresholds["per_day"] * 100, 1),
                "total_calls": len(self.records),
            }

    def get_summary(self) -> dict:
        """获取成本汇总"""
        total = sum(r.cost_usd for r in self.records)
        by_provider = {}
        by_stage = {}
        for r in self.records:
            by_provider.setdefault(r.provider, {"cost": 0, "calls": 0})
            by_provider[r.provider]["cost"] += r.cost_usd
            by_provider[r.provider]["calls"] += 1

            by_stage.setdefault(r.stage, {"cost": 0, "calls": 0})
            by_stage[r.stage]["cost"] += r.cost_usd
            by_stage[r.stage]["calls"] += 1

        return {
            "total_cost_usd": round(total, 4),
            "total_calls": len(self.records),
            "by_provider": by_provider,
            "by_stage": by_stage,
            "recent_alerts": self._alerts[-5:] if self._alerts else [],
        }


# ========== Provider 抽象基类 ==========

class BaseProvider(ABC):
    """模型Provider抽象基类"""

    provider_name: str = "base"

    def __init__(self, cost_tracker: CostTracker = None):
        self.cost_tracker = cost_tracker or CostTracker()

    @abstractmethod
    def analyze(self, prompt: str, input_data: dict, stage: str, model: str = None) -> dict:
        """
        统一分析方法

        Args:
            prompt: 完整的Prompt文本
            input_data: 输入数据（视频元数据、上游输出等）
            stage: 流水线阶段（P0-P5 或 skill名）
            model: 可选覆盖模型名

        Returns:
            dict: 结构化分析结果，必须包含 pipeline_stage 和 output
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """获取默认模型"""
        pass

    def _build_user_message(self, prompt: str, input_data: dict) -> str:
        """将Prompt + 输入数据组装为用户消息"""
        parts = [prompt]
        if input_data:
            parts.append("\n\n--- 输入数据 ---\n")
            parts.append(json.dumps(input_data, ensure_ascii=False, indent=2))
        return "".join(parts)

    def _pre_call_check(self, stage: str):
        """API调用前预算检查"""
        try:
            status = self.cost_tracker.check_budget()
            if status["level"] != "ok":
                logger.warning(f"[BudgetGuard] {stage}: {status['message']}")
        except BudgetExceededError as e:
            logger.critical(f"[BudgetGuard] {stage}: {e}")
            raise  # 阻止调用


# ========== Claude Provider ==========

class ClaudeProvider(BaseProvider):
    """Claude模型Provider（通过anthropic SDK）"""

    provider_name = "claude"

    def __init__(self, api_key: str = None, base_url: str = None,
                 default_model: str = None, cost_tracker: CostTracker = None):
        super().__init__(cost_tracker)
        from anthropic import Anthropic
        kwargs = {"api_key": api_key or os.getenv("ANTHROPIC_API_KEY")}
        base = base_url or os.getenv("ANTHROPIC_BASE_URL")
        if base:
            kwargs["base_url"] = base
        self.client = Anthropic(**kwargs)
        self.default_model = default_model or os.getenv("MODEL", "claude-haiku-4-5")
        self.max_retries = 3

    def get_default_model(self) -> str:
        return self.default_model

    def analyze(self, prompt: str, input_data: dict, stage: str, model: str = None) -> dict:
        """调用Claude进行分析"""
        model = model or self.default_model
        user_message = self._build_user_message(prompt, input_data)

        try:
            result = self._call_claude(model, user_message, stage)
            return result
        except Exception as e:
            logger.error(f"[Claude] 调用失败(重试耗尽): {e}")
            return {
                "pipeline_stage": stage,
                "error": f"Claude调用失败: {str(e)[:200]}",
                "output": {},
            }

    @with_retry(max_retries=3, base_delay=2.0, max_delay=30.0, timeout=120.0)
    def _call_claude(self, model: str, user_message: str, stage: str) -> dict:
        """实际的Claude API调用（被@with_retry装饰）"""
        self._pre_call_check(stage)  # 预算守门
        start_time = time.time()

        response = self.client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_message}],
        )

        latency = int((time.time() - start_time) * 1000)

        # 解析结果 - 跳过thinking block，找到第一个text block
        result_text = ""
        for block in response.content:
            if hasattr(block, 'text') and block.type == "text":
                result_text = block.text
                break

        if not result_text:
            result_text = str(response.content)

        result = self._parse_json_result(result_text, stage)

        # 记录成本
        usage = response.usage
        pricing = CostTracker.PRICING.get(model, {"input": 0.001, "output": 0.005})
        cost = (usage.input_tokens / 1000 * pricing["input"] +
                usage.output_tokens / 1000 * pricing["output"])

        self.cost_tracker.record(CostRecord(
            provider=self.provider_name,
            model=model,
            stage=stage,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
            latency_ms=latency,
            success=True,
        ))

        # 返回结果带token统计
        result["_token_stats"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": round(cost, 6),
        }
        return result

    def _parse_json_result(self, text: str, stage: str) -> dict:
        """尝试从Claude回复中解析JSON结果"""
        # 尝试直接解析
        try:
            result = json.loads(text)
            if "pipeline_stage" in result or "output" in result:
                return result
        except json.JSONDecodeError:
            pass

        # 尝试提取JSON代码块
        import re
        json_match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group(1))
                if "pipeline_stage" in result or "output" in result:
                    return result
            except json.JSONDecodeError:
                pass

        # 尝试提取最外层{...}
        brace_match = re.search(r'\{.*\}', text, re.DOTALL)
        if brace_match:
            try:
                result = json.loads(brace_match.group(0))
                if "pipeline_stage" in result or "output" in result:
                    return result
            except json.JSONDecodeError:
                pass

        # 都失败，包装原始文本
        return {
            "pipeline_stage": stage,
            "output": {
                "raw_text": text,
                "parse_warning": "未能解析为结构化JSON，返回原始文本"
            },
        }


# ========== Gemini Provider ==========

class GeminiProvider(BaseProvider):
    """
    Gemini模型Provider - 支持文本+视频原生理解
    使用 google-genai SDK (AI Studio 免费 tier)
    """

    provider_name = "gemini"

    def __init__(self, api_key: str = None, default_model: str = None,
                 cost_tracker: CostTracker = None):
        super().__init__(cost_tracker)
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.default_model = default_model or "gemini-2.5-flash"
        self._available = bool(self.api_key)
        self.max_retries = 3

        if self._available:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
        else:
            self.client = None

    def get_default_model(self) -> str:
        return self.default_model

    @property
    def is_available(self) -> bool:
        """检查Gemini是否可用"""
        return self._available

    def analyze(self, prompt: str, input_data: dict, stage: str, model: str = None) -> dict:
        """调用Gemini进行分析（支持文本+视频）"""
        if not self._available:
            return {
                "pipeline_stage": stage,
                "error": "Gemini Provider 未配置 API Key",
                "output": {},
            }

        model = model or self.default_model
        contents = self._build_contents(prompt, input_data)

        try:
            result = self._call_gemini(model, contents, stage)
            return result
        except Exception as e:
            logger.error(f"[Gemini] 调用失败(重试耗尽): {e}")
            return {
                "pipeline_stage": stage,
                "error": f"Gemini调用失败: {str(e)[:200]}",
                "output": {},
            }

    @with_retry(max_retries=3, base_delay=2.0, max_delay=30.0, timeout=120.0)
    def _call_gemini(self, model: str, contents: list, stage: str) -> dict:
        """实际的Gemini API调用（被@with_retry装饰）"""
        self._pre_call_check(stage)  # 预算守门
        start_time = time.time()

        from google import genai

        response = self.client.models.generate_content(
            model=model,
            contents=contents,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=4096,
            ),
        )

        latency = int((time.time() - start_time) * 1000)
        result_text = response.text or "{}"
        result = self._parse_json_result(result_text, stage)

        um = response.usage_metadata
        input_tokens = um.prompt_token_count if um else 0
        output_tokens = um.candidates_token_count if um else 0
        pricing = CostTracker.PRICING.get(model, {"input": 0.00015, "output": 0.0006})
        cost = (input_tokens / 1000 * pricing["input"] +
                output_tokens / 1000 * pricing["output"])

        self.cost_tracker.record(CostRecord(
            provider=self.provider_name,
            model=model,
            stage=stage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency,
            success=True,
        ))

        result["_token_stats"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
        }
        return result

    def _build_contents(self, prompt: str, input_data: dict):
        """构建Gemini contents（支持视频上传）"""
        contents = []

        # 检查是否有真实视频文件路径
        video_path = None
        if input_data:
            # 从 input_data 或 video_input 中查找视频路径
            video_path = input_data.get("video_path")
            if not video_path and isinstance(input_data.get("video_input"), dict):
                video_path = input_data["video_input"].get("video_path")

        if video_path and os.path.exists(video_path):
            # 上传视频文件
            try:
                video_file = self.client.files.upload(file=video_path)
                contents.append(video_file)
                logger.info(f"[Gemini] 已上传视频: {video_path}")
            except Exception as e:
                logger.warning(f"[Gemini] 视频上传失败: {e}, 降级为文本分析")

        # Prompt + 结构化输入数据
        text_parts = [prompt]
        if input_data:
            text_parts.append("\n\n--- 输入数据 ---\n")
            text_parts.append(json.dumps(input_data, ensure_ascii=False, indent=2))

        contents.append("".join(text_parts))
        return contents

    def _parse_json_result(self, text: str, stage: str) -> dict:
        """尝试从Gemini回复中解析JSON结果"""
        # 尝试直接解析
        try:
            result = json.loads(text)
            if "pipeline_stage" in result or "output" in result:
                return result
        except json.JSONDecodeError:
            pass

        # 尝试提取JSON代码块
        import re
        json_match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group(1))
                if "pipeline_stage" in result or "output" in result:
                    return result
            except json.JSONDecodeError:
                pass

        # 尝试提取最外层{...}
        brace_match = re.search(r'\{.*\}', text, re.DOTALL)
        if brace_match:
            try:
                result = json.loads(brace_match.group(0))
                if "pipeline_stage" in result or "output" in result:
                    return result
            except json.JSONDecodeError:
                pass

        # 都失败，包装原始文本
        return {
            "pipeline_stage": stage,
            "output": {
                "raw_text": text,
                "parse_warning": "未能解析为结构化JSON，返回原始文本",
            },
        }


# ========== QwenVL Provider ==========

class QwenVLProvider(BaseProvider):
    """
    Qwen3-VL-Flash 多模态Provider - 支持图片+文本
    通过阿里云百炼 OpenAI兼容接口接入
    用于P1抽帧分析和P4交叉验证
    """

    provider_name = "qwenvl"

    def __init__(self, api_key: str = None, base_url: str = None,
                 default_model: str = None, cost_tracker: CostTracker = None):
        super().__init__(cost_tracker)
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = base_url or os.getenv("DASHSCOPE_BASE_URL",
                                               "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.default_model = default_model or os.getenv("QWEN_MODEL", "qwen3-vl-flash")
        self._available = bool(self.api_key)
        self.max_retries = 3

        if self._available:
            from openai import OpenAI
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        else:
            self.client = None

    def get_default_model(self) -> str:
        return self.default_model

    @property
    def is_available(self) -> bool:
        return self._available

    def analyze(self, prompt: str, input_data: dict, stage: str, model: str = None) -> dict:
        """调用Qwen-VL进行分析（支持图片URL + 文本）"""
        if not self._available:
            return {
                "pipeline_stage": stage,
                "error": "QwenVL Provider 未配置 DASHSCOPE_API_KEY",
                "output": {},
            }

        model = model or self.default_model
        messages = self._build_messages(prompt, input_data)

        try:
            result = self._call_qwenvl(model, messages, stage)
            return result
        except Exception as e:
            logger.error(f"[QwenVL] 调用失败(重试耗尽): {e}")
            return {
                "pipeline_stage": stage,
                "error": f"QwenVL调用失败: {str(e)[:200]}",
                "output": {},
            }

    @with_retry(max_retries=3, base_delay=2.0, max_delay=30.0, timeout=120.0)
    def _call_qwenvl(self, model: str, messages: list, stage: str) -> dict:
        """实际的QwenVL API调用（被@with_retry装饰）"""
        self._pre_call_check(stage)  # 预算守门
        start_time = time.time()

        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=4096,
        )

        latency = int((time.time() - start_time) * 1000)
        result_text = response.choices[0].message.content or "{}"
        result = self._parse_json_result(result_text, stage)

        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        pricing = CostTracker.PRICING.get(model, {"input": 0.0001, "output": 0.0003})
        cost = (input_tokens / 1000 * pricing["input"] +
                output_tokens / 1000 * pricing["output"])

        self.cost_tracker.record(CostRecord(
            provider=self.provider_name,
            model=model,
            stage=stage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency,
            success=True,
        ))

        result["_token_stats"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
        }
        return result

    def _build_messages(self, prompt: str, input_data: dict) -> list:
        """
        构建OpenAI兼容格式的messages
        支持图片URL（来自抽帧数据 frame_urls）
        """
        content = []

        # 检查是否有抽帧图片URL
        frame_urls = []
        if input_data:
            frame_urls = input_data.get("frame_urls", [])
            if not frame_urls and isinstance(input_data.get("video_input"), dict):
                frame_urls = input_data["video_input"].get("frame_urls", [])

        for url in frame_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url},
            })

        # 文本部分：Prompt + 输入数据
        text_parts = [prompt]
        if input_data:
            # 排除frame_urls，避免重复
            clean_data = {k: v for k, v in input_data.items() if k != "frame_urls"}
            if clean_data:
                text_parts.append("\n\n--- 输入数据 ---\n")
                text_parts.append(json.dumps(clean_data, ensure_ascii=False, indent=2))

        content.append({"type": "text", "text": "".join(text_parts)})

        return [{"role": "user", "content": content}]

    def _parse_json_result(self, text: str, stage: str) -> dict:
        """尝试从Qwen回复中解析JSON结果"""
        try:
            result = json.loads(text)
            if "pipeline_stage" in result or "output" in result:
                return result
        except json.JSONDecodeError:
            pass

        import re
        json_match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group(1))
                if "pipeline_stage" in result or "output" in result:
                    return result
            except json.JSONDecodeError:
                pass

        brace_match = re.search(r'\{.*\}', text, re.DOTALL)
        if brace_match:
            try:
                result = json.loads(brace_match.group(0))
                if "pipeline_stage" in result or "output" in result:
                    return result
            except json.JSONDecodeError:
                pass

        return {
            "pipeline_stage": stage,
            "output": {
                "raw_text": text,
                "parse_warning": "未能解析为结构化JSON，返回原始文本",
            },
        }


# ========== Provider工厂 ==========

_providers: dict[str, BaseProvider] = {}
_cost_tracker: Optional[CostTracker] = None


def get_cost_tracker() -> CostTracker:
    """获取全局成本追踪器"""
    global _cost_tracker
    if _cost_tracker is None:
        _cost_tracker = CostTracker()
    return _cost_tracker


def get_provider(name: str = "claude") -> BaseProvider:
    """
    获取Provider实例（单例模式）

    Args:
        name: provider名（claude / gemini）

    Returns:
        BaseProvider实例
    """
    global _providers

    if name not in _providers:
        tracker = get_cost_tracker()
        if name == "claude":
            _providers[name] = ClaudeProvider(cost_tracker=tracker)
        elif name == "gemini":
            _providers[name] = GeminiProvider(cost_tracker=tracker)
        elif name == "qwenvl":
            _providers[name] = QwenVLProvider(cost_tracker=tracker)
        else:
            raise ValueError(f"未知Provider: {name}")

    return _providers[name]


def reset_providers():
    """重置所有Provider（测试用）"""
    global _providers, _cost_tracker
    _providers = {}
    _cost_tracker = None
