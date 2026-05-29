"""
AI编导Agent - P0-P5视频分析Pipeline编排调度器
基于DAG依赖图的串/并行调度 + 双模型路由 + 失败重试 + 成本监控
"""

import json
import time
import asyncio
import logging
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from agent.provider import get_provider, get_cost_tracker, BaseProvider
from agent.prompt_manager import get_prompt_manager
from agent.pipeline_schema import validate_stage_output
from agent import storage

logger = logging.getLogger(__name__)


# ========== Pipeline阶段定义 ==========

class PipelineStage(str, Enum):
    """P0-P5阶段枚举"""
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"


# 阶段对应的Prompt名和Provider偏好
STAGE_CONFIG = {
    PipelineStage.P0: {
        "prompt_name": "pipeline_P0_元数据识别",
        "preferred_provider": "claude",
        "fallback_provider": "claude",
        "timeout_sec": 30,
    },
    PipelineStage.P1: {
        "prompt_name": "pipeline_P1_视觉要素",
        "preferred_provider": "gemini",      # P1: Gemini原生视频分析
        "fallback_provider": "claude",
        "cross_validation_provider": "qwenvl",  # P1: QwenVL抽帧分析（交叉验证）
        "timeout_sec": 60,
    },
    PipelineStage.P2: {
        "prompt_name": "pipeline_P2_文案音频",
        "preferred_provider": "claude",
        "fallback_provider": "claude",
        "timeout_sec": 60,
    },
    PipelineStage.P3: {
        "prompt_name": "pipeline_P3_结构叙事",
        "preferred_provider": "claude",
        "fallback_provider": "claude",
        "timeout_sec": 60,
    },
    PipelineStage.P4: {
        "prompt_name": "pipeline_P4_合规效果",
        "preferred_provider": "claude",
        "fallback_provider": "claude",
        "timeout_sec": 60,
    },
    PipelineStage.P5: {
        "prompt_name": "pipeline_P5_结构化入库",
        "preferred_provider": "claude",
        "fallback_provider": "claude",
        "timeout_sec": 60,
    },
}

# 阶段级重试配置
STAGE_MAX_RETRIES = 2  # 阶段失败后最多重试2次（不含首次）

# DAG依赖图（定义阶段间的执行依赖）
STAGE_DEPENDENCIES = {
    PipelineStage.P0: [],                                      # P0无依赖
    PipelineStage.P1: [PipelineStage.P0],                      # P1依赖P0
    PipelineStage.P2: [PipelineStage.P0],                      # P2依赖P0（P1和P2可并行）
    PipelineStage.P3: [PipelineStage.P1, PipelineStage.P2],    # P3依赖P1+P2
    PipelineStage.P4: [PipelineStage.P3],                      # P4依赖P3
    PipelineStage.P5: [PipelineStage.P4],                      # P5依赖P4
}

# 阶段执行顺序（拓扑排序）
EXECUTION_ORDER = [
    [PipelineStage.P0],                       # 第一层：P0
    [PipelineStage.P1, PipelineStage.P2],     # 第二层：P1/P2并行
    [PipelineStage.P3],                       # 第三层：P3
    [PipelineStage.P4],                       # 第四层：P4
    [PipelineStage.P5],                       # 第五层：P5
]


# ========== Pipeline运行状态 ==========

class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StageResult:
    """单个阶段的执行结果"""
    stage: PipelineStage
    status: StageStatus = StageStatus.PENDING
    result: dict = field(default_factory=dict)
    error: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    started_at: float = 0
    completed_at: float = 0
    # 交叉验证：第二个模型的结果
    cross_validation_result: dict = field(default_factory=dict)
    cross_validation_provider: str = ""
    cross_validation_model: str = ""


@dataclass
class PipelineRun:
    """一次完整的Pipeline运行"""
    run_id: str = ""
    video_input: dict = field(default_factory=dict)
    stage_results: dict[PipelineStage, StageResult] = field(default_factory=dict)
    status: str = "pending"     # pending / running / completed / failed
    started_at: float = 0
    completed_at: float = 0
    total_cost_usd: float = 0.0

    def __post_init__(self):
        # 初始化所有阶段
        for stage in PipelineStage:
            if stage not in self.stage_results:
                self.stage_results[stage] = StageResult(stage=stage)


# ========== Pipeline调度器 ==========

class PipelineOrchestrator:
    """
    P0-P5视频分析Pipeline编排调度器

    核心能力：
    1. DAG依赖调度：P0→[P1||P2]→P3→P4→P5
    2. 双模型路由：per-stage选择Claude/Gemini
    3. 失败重试：自动fallback到备用Provider
    4. 成本监控：实时追踪每个阶段的成本
    5. element_id引用链：上游输出自动注入下游输入
    """

    def __init__(self):
        self.pm = get_prompt_manager()
        self.cost_tracker = get_cost_tracker()
        self._runs: dict[str, PipelineRun] = {}
        self._executor = ThreadPoolExecutor(max_workers=3)  # P1/P2并行

    def run(self, video_input: dict, run_id: str = None,
            progress_callback: callable = None) -> PipelineRun:
        """
        执行完整的P0-P5 Pipeline（同步）

        Args:
            video_input: 视频输入数据（元数据或模拟数据）
            run_id: 可选运行ID

        Returns:
            PipelineRun: 完整运行结果
        """
        # 确保.env已加载
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / ".env")

        run_id = run_id or f"run_{int(time.time())}"
        run = PipelineRun(run_id=run_id, video_input=video_input)
        run.started_at = time.time()
        run.status = "running"
        self._runs[run_id] = run

        logger.info(f"[Pipeline] 开始运行 {run_id}")

        try:
            # 按拓扑层执行
            for layer_idx, layer in enumerate(EXECUTION_ORDER):
                logger.info(f"[Pipeline] Layer {layer_idx}: {[s.value for s in layer]}")

                # 通知层开始
                for stage in layer:
                    if progress_callback:
                        progress_callback(stage.value, "running")

                if len(layer) == 1:
                    # 串行执行
                    stage = layer[0]
                    self._execute_stage(run, stage)
                    # 通知阶段完成/失败
                    if progress_callback:
                        sr = run.stage_results[stage]
                        progress_callback(stage.value, sr.status.value)
                else:
                    # 并行执行
                    self._execute_stages_parallel(run, layer)
                    # 通知每个阶段的结果
                    for stage in layer:
                        if progress_callback:
                            sr = run.stage_results[stage]
                            progress_callback(stage.value, sr.status.value)

                # 检查是否有阶段失败（导致下游无法执行）
                failed = [s for s in layer if run.stage_results[s].status == StageStatus.FAILED]
                if failed:
                    # 标记所有下游为SKIPPED
                    self._skip_downstream(run, failed)
                    break

            # 最终状态
            all_completed = all(
                run.stage_results[s].status == StageStatus.COMPLETED
                for s in PipelineStage
            )
            run.status = "completed" if all_completed else "failed"

        except Exception as e:
            logger.error(f"[Pipeline] 运行异常: {e}")
            run.status = "failed"

        from agent.reference_tracker import extract_and_register_references

        run.completed_at = time.time()
        run.total_cost_usd = self.cost_tracker.get_summary()["total_cost_usd"]

        logger.info(f"[Pipeline] 运行完成 {run_id}: status={run.status}, cost=${run.total_cost_usd:.4f}")

        # 提取并注册跨Prompt引用关系
        try:
            stage_outputs = {
                s.value: {"output": run.stage_results[s].result.get("output", {})
                          if run.stage_results[s].result else {}}
                for s in PipelineStage
            }
            ref_result = extract_and_register_references(run_id, stage_outputs)
            logger.info(f"[Pipeline] 引用注册完成: {ref_result['registered']}条引用"
                        + (f", {len(ref_result['errors'])}个错误" if ref_result["errors"] else ""))
        except Exception as e:
            logger.warning(f"[Pipeline] 引用注册失败: {e}")

        # Novel标签捕获
        try:
            from agent.novel_capture import on_pipeline_complete
            novel_result = on_pipeline_complete(run_id, stage_outputs)
            if novel_result["captured"] > 0:
                logger.info(f"[Pipeline] Novel捕获: {novel_result['captured']}个新候选标签")
        except Exception as e:
            logger.warning(f"[Pipeline] Novel捕获失败: {e}")

        # 检查成本告警
        alerts = self.cost_tracker.get_alerts()
        if alerts:
            for alert in alerts:
                logger.warning(f"[成本告警] {alert['message']}")

        return run

    def _execute_stage(self, run: PipelineRun, stage: PipelineStage):
        """执行单个阶段（含阶段级重试 + 交叉验证）"""
        config = STAGE_CONFIG[stage]
        sr = run.stage_results[stage]

        # 阶段级重试循环
        for stage_attempt in range(STAGE_MAX_RETRIES + 1):
            try:
                self._execute_stage_once(run, stage, config, sr)
                if sr.status == StageStatus.COMPLETED:
                    return  # 成功，退出
            except Exception as e:
                logger.error(f"[Pipeline] {stage.value} 阶段异常(第{stage_attempt+1}次): {e}")
                sr.error = str(e)

            if sr.status == StageStatus.FAILED and stage_attempt < STAGE_MAX_RETRIES:
                wait = 2 ** (stage_attempt + 1)
                logger.warning(
                    f"[Pipeline] {stage.value} 阶段失败, 阶段级重试 {stage_attempt+1}/{STAGE_MAX_RETRIES}, "
                    f"等待{wait}s"
                )
                time.sleep(wait)
                # 重置状态以准备重试
                sr.status = StageStatus.PENDING
                sr.error = ""
                sr.result = {}

        # 所有重试耗尽
        sr.status = StageStatus.FAILED
        sr.completed_at = time.time()
        sr.latency_ms = int((sr.completed_at - sr.started_at) * 1000)

    def _execute_stage_once(self, run: PipelineRun, stage: PipelineStage,
                             config: dict, sr: StageResult):
        """执行单个阶段一次（不含阶段级重试）"""
        sr.status = StageStatus.RUNNING
        if not sr.started_at:
            sr.started_at = time.time()  # 首次记录，重试时保留

        # 组装输入：视频原始输入 + 上游依赖输出
        stage_input = self._build_stage_input(run, stage)

        # 加载Prompt
        try:
            prompt = self.pm.load_prompt(config["prompt_name"])
        except Exception as e:
            sr.status = StageStatus.FAILED
            sr.error = f"Prompt加载失败: {e}"
            sr.completed_at = time.time()
            sr.latency_ms = int((sr.completed_at - sr.started_at) * 1000)
            return

        # ===== 主Provider调用 =====
        provider_name = config["preferred_provider"]
        provider = get_provider(provider_name)

        logger.info(f"[Pipeline] {stage.value} → {provider_name}({provider.get_default_model()})")
        result = provider.analyze(
            prompt=prompt,
            input_data=stage_input,
            stage=stage.value,
        )

        # 检查主Provider结果，失败则fallback
        if "error" in result:
            fallback_name = config.get("fallback_provider")
            if fallback_name and fallback_name != provider_name:
                logger.warning(f"[Pipeline] {stage.value} 主Provider失败，尝试fallback → {fallback_name}")
                fallback_provider = get_provider(fallback_name)
                result = fallback_provider.analyze(
                    prompt=prompt,
                    input_data=stage_input,
                    stage=stage.value,
                )
                sr.provider = fallback_name
                sr.model = fallback_provider.get_default_model()
            else:
                sr.provider = provider_name
                sr.model = provider.get_default_model()

            if "error" in result:
                sr.status = StageStatus.FAILED
                sr.error = result["error"]
                sr.result = result
                sr.completed_at = time.time()
                sr.latency_ms = int((sr.completed_at - sr.started_at) * 1000)
                return
        else:
            sr.provider = provider_name
            sr.model = provider.get_default_model()

        # ===== 交叉验证（如有配置） =====
        cv_provider_name = config.get("cross_validation_provider")
        if cv_provider_name:
            try:
                cv_provider = get_provider(cv_provider_name)
                logger.info(f"[Pipeline] {stage.value} 交叉验证 → {cv_provider_name}({cv_provider.get_default_model()})")
                cv_result = cv_provider.analyze(
                    prompt=prompt,
                    input_data=stage_input,
                    stage=f"{stage.value}_cv",
                )
                if "error" not in cv_result:
                    sr.cross_validation_result = cv_result
                    sr.cross_validation_provider = cv_provider_name
                    sr.cross_validation_model = cv_provider.get_default_model()
                    logger.info(f"[Pipeline] {stage.value} 交叉验证完成")
                else:
                    logger.warning(f"[Pipeline] {stage.value} 交叉验证失败: {cv_result.get('error', '')[:80]}")
            except Exception as e:
                logger.warning(f"[Pipeline] {stage.value} 交叉验证异常: {e}")

        # ===== 组装最终结果（合并交叉验证数据） =====
        sr.result = result

        # 如果有交叉验证结果，注入到主结果中供P4比对
        if sr.cross_validation_result:
            sr.result["cross_validation"] = {
                "provider": sr.cross_validation_provider,
                "model": sr.cross_validation_model,
                "output": sr.cross_validation_result.get("output", {}),
            }

        schema_validation = validate_stage_output(stage.value, sr.result, prompt_manager=self.pm)
        sr.result["_schema_validation"] = schema_validation
        if not schema_validation["valid"]:
            logger.warning(
                f"[Pipeline] {stage.value} 输出Schema校验未通过: "
                f"{'; '.join(schema_validation['errors'])}"
            )

        sr.status = StageStatus.COMPLETED
        sr.completed_at = time.time()
        sr.latency_ms = int((sr.completed_at - sr.started_at) * 1000)
        logger.info(f"[Pipeline] {stage.value} 完成, 耗时{sr.latency_ms}ms")

        # 持久化成本记录（从Provider返回值提取实际token数）
        try:
            token_stats = sr.result.get("_token_stats", {})
            storage.save_cost_record(
                provider=sr.provider,
                model=sr.model,
                stage=stage.value,
                input_tokens=token_stats.get("input_tokens", 0),
                output_tokens=token_stats.get("output_tokens", 0),
                cost_usd=token_stats.get("cost_usd", 0.0),
                latency_ms=sr.latency_ms,
                success=True,
            )
        except Exception as e:
            logger.warning(f"[Pipeline] 成本记录保存失败: {e}")

    def _execute_stages_parallel(self, run: PipelineRun, stages: list[PipelineStage]):
        """并行执行多个阶段"""
        futures = {}
        for stage in stages:
            future = self._executor.submit(self._execute_stage, run, stage)
            futures[stage] = future

        # 等待所有并行阶段完成
        for stage, future in futures.items():
            try:
                future.result(timeout=STAGE_CONFIG[stage]["timeout_sec"])
            except Exception as e:
                logger.error(f"[Pipeline] {stage.value} 并行执行异常: {e}")
                run.stage_results[stage].status = StageStatus.FAILED
                run.stage_results[stage].error = str(e)

    def _build_stage_input(self, run: PipelineRun, stage: PipelineStage) -> dict:
        """组装阶段的输入数据（视频原始输入 + 上游输出）"""
        inputs = {"video_input": run.video_input}

        # 注入上游依赖的输出
        deps = STAGE_DEPENDENCIES[stage]
        for dep_stage in deps:
            dep_result = run.stage_results[dep_stage]
            if dep_result.status == StageStatus.COMPLETED and dep_result.result:
                inputs[f"{dep_stage.value}_output"] = dep_result.result

        return inputs

    def _skip_downstream(self, run: PipelineRun, failed_stages: list[PipelineStage]):
        """标记失败阶段的所有下游为SKIPPED"""
        # 简单实现：所有后续层都标记为SKIPPED
        found_failed = False
        for layer in EXECUTION_ORDER:
            if any(s in failed_stages for s in layer):
                found_failed = True
                continue
            if found_failed:
                for stage in layer:
                    run.stage_results[stage].status = StageStatus.SKIPPED
                    run.stage_results[stage].error = f"上游阶段{[s.value for s in failed_stages]}失败"

    # ========== 查询方法 ==========

    def get_run(self, run_id: str) -> Optional[PipelineRun]:
        """获取运行结果"""
        return self._runs.get(run_id)

    def list_runs(self) -> list[dict]:
        """列出所有运行"""
        return [
            {
                "run_id": r.run_id,
                "status": r.status,
                "started_at": r.started_at,
                "completed_at": r.completed_at,
                "total_cost_usd": r.total_cost_usd,
                "stages": {
                    s.value: {"status": sr.status.value, "latency_ms": sr.latency_ms}
                    for s, sr in r.stage_results.items()
                },
            }
            for r in self._runs.values()
        ]

    def get_cost_summary(self) -> dict:
        """获取成本汇总"""
        return self.cost_tracker.get_summary()

    def get_alerts(self) -> list[dict]:
        """获取成本告警"""
        return self.cost_tracker.get_alerts(clear=False)


# ========== 全局单例 ==========

_orchestrator: Optional[PipelineOrchestrator] = None


def get_orchestrator() -> PipelineOrchestrator:
    """获取全局Pipeline调度器"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = PipelineOrchestrator()
    return _orchestrator


def reset_orchestrator():
    """重置调度器（测试用）"""
    global _orchestrator
    _orchestrator = None
