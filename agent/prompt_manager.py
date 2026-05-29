"""
AI编导Agent - Prompt版本管理器
解决评估材料难点1（Prompt版本↔输出Schema绑定）和难点2（跨Prompt引用追溯）

核心能力：
1. 版本化Prompt加载 - 按manifest自动选择最新版
2. Schema兼容性检查 - 版本变更时验证兼容性
3. 跨Prompt引用追溯 - 查询某个element_id被谁引用
4. 版本印戳注入 - 输出数据带版本标识，保证历史数据可解释
5. Prompt升级工具 - 安全升级Prompt版本，自动生成迁移方案
"""

import json
import copy
from pathlib import Path
from typing import Optional
from datetime import datetime


PROMPTS_ROOT = Path(__file__).parent.parent / "prompts"
MANIFEST_PATH = PROMPTS_ROOT / "manifest.json"


class PromptManager:
    """Prompt版本管理器"""

    def __init__(self, manifest_path: str = None):
        self.manifest_path = Path(manifest_path) if manifest_path else MANIFEST_PATH
        self._manifest: dict = {}
        self._load_manifest()

    # ========== Manifest 读写 ==========

    def _load_manifest(self):
        """加载manifest.json"""
        if self.manifest_path.exists():
            self._manifest = json.loads(
                self.manifest_path.read_text(encoding="utf-8")
            )
        else:
            self._manifest = {
                "version": "1.0.0",
                "last_updated": datetime.now().strftime("%Y-%m-%d"),
                "active_version": "v1",
                "prompts": {},
                "schema_compatibility_matrix": {},
                "cross_prompt_references": {},
            }

    def _save_manifest(self):
        """保存manifest.json"""
        self._manifest["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        self.manifest_path.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def active_version(self) -> str:
        """当前激活的Prompt版本目录（如 v1, v2）"""
        return self._manifest.get("active_version", "v1")

    # ========== Prompt 加载 ==========

    def load_prompt(self, prompt_name: str, version_dir: str = None) -> str:
        """
        加载指定Prompt的内容
        优先使用指定version_dir，否则使用manifest中的active_version
        """
        ver = version_dir or self.active_version
        prompt_dir = PROMPTS_ROOT / ver

        # 从manifest获取文件名
        prompt_info = self._manifest.get("prompts", {}).get(prompt_name, {})
        filename = prompt_info.get("file", f"{prompt_name}.md")

        filepath = prompt_dir / filename
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")
            # 注入版本印戳
            version_stamp = prompt_info.get("version", "unknown")
            schema_version = prompt_info.get("schema_version", "unknown")
            stamped = self._inject_version_stamp(content, prompt_name, version_stamp, schema_version)
            return stamped

        return f"[Prompt未找到: {ver}/{filename}]"

    def _inject_version_stamp(self, content: str, prompt_name: str,
                               version: str, schema_version: str) -> str:
        """
        在Prompt内容末尾注入版本印戳
        这个印戳会被Agent带入输出数据，保证历史数据可追溯到Prompt版本
        """
        stamp = (
            f"\n\n---\n"
            f"<!-- PROMPT_VERSION_STAMP\n"
            f"prompt: {prompt_name}\n"
            f"version: {version}\n"
            f"schema_version: {schema_version}\n"
            f"loaded_at: {datetime.now().isoformat()}\n"
            f"END_STAMP -->"
        )
        return content + stamp

    # ========== Schema 兼容性 ==========

    def check_schema_compatibility(self, from_version: str, to_version: str) -> dict:
        """
        检查两个Schema版本之间的兼容性
        返回: {compatible: bool, breaking_changes: list, migration_notes: str}
        """
        matrix = self._manifest.get("schema_compatibility_matrix", {})
        from_info = matrix.get(from_version, {})

        if to_version in from_info.get("compatible_with", []):
            return {
                "compatible": True,
                "breaking_changes": [],
                "migration_notes": "兼容，无需迁移",
            }

        breaking = from_info.get("breaking_changes", [])
        migrate = from_info.get("migrate_from", {})

        return {
            "compatible": False,
            "breaking_changes": breaking,
            "migration_notes": f"存在{len(breaking)}个Breaking Change，需要迁移。迁移逻辑: {json.dumps(migrate, ensure_ascii=False)}",
        }

    def get_prompt_version(self, prompt_name: str) -> dict:
        """获取指定Prompt的版本信息"""
        info = self._manifest.get("prompts", {}).get(prompt_name, {})
        return {
            "name": prompt_name,
            "version": info.get("version", "unknown"),
            "schema_version": info.get("schema_version", "unknown"),
            "description": info.get("description", ""),
            "dependencies": info.get("dependencies", []),
            "file": info.get("file", ""),
        }

    def get_all_versions(self) -> list[dict]:
        """获取所有Prompt的版本信息"""
        prompts = self._manifest.get("prompts", {})
        return [
            {
                "name": name,
                "version": info.get("version", "unknown"),
                "schema_version": info.get("schema_version", "unknown"),
                "description": info.get("description", ""),
                "dependencies": info.get("dependencies", []),
            }
            for name, info in prompts.items()
        ]

    # ========== 跨Prompt引用追溯 ==========

    def trace_references(self, prompt_name: str) -> dict:
        """
        追溯指定Prompt的引用关系
        返回: {output_elements, referenced_by, references_to}
        """
        refs = self._manifest.get("cross_prompt_references", {})
        prompt_refs = refs.get(prompt_name, {})

        # 找出该Prompt引用了谁（从dependencies反查）
        prompts = self._manifest.get("prompts", {})
        deps = prompts.get(prompt_name, {}).get("dependencies", [])

        return {
            "prompt": prompt_name,
            "output_elements": prompt_refs.get("output_elements", []),
            "referenced_by": prompt_refs.get("referenced_by", []),
            "references_to": deps,
        }

    def find_element_source(self, element_id: str) -> Optional[dict]:
        """
        根据element_id前缀找到产出它的Prompt
        element_id格式: PREFIX_序号 (如 NEED_001, COPY_003)
        """
        prefix = element_id.rsplit("_", 1)[0] if "_" in element_id else element_id

        prompts = self._manifest.get("prompts", {})
        refs = self._manifest.get("cross_prompt_references", {})

        for name, info in prompts.items():
            elem_prefix = info.get("output_schema", {}).get("element_id_prefix", "")
            if elem_prefix and prefix.startswith(elem_prefix):
                ref_info = refs.get(name, {})
                return {
                    "source_prompt": name,
                    "element_id": element_id,
                    "version": info.get("version", "unknown"),
                    "schema_version": info.get("schema_version", "unknown"),
                    "referenced_by": ref_info.get("referenced_by", []),
                }

        return None

    def who_uses_element(self, element_id: str) -> list[str]:
        """查询哪些Prompt引用了指定element_id"""
        source = self.find_element_source(element_id)
        if source:
            return source["referenced_by"]
        return []

    # ========== Prompt 升级 ==========

    def upgrade_prompt(self, prompt_name: str, new_version: str,
                       new_schema_version: str = None,
                       changelog_entry: str = "",
                       breaking_changes: list = None) -> dict:
        """
        升级Prompt版本（仅更新manifest，不修改文件内容）
        返回升级结果和影响分析
        """
        prompts = self._manifest.get("prompts", {})
        if prompt_name not in prompts:
            return {"success": False, "error": f"Prompt未注册: {prompt_name}"}

        old_info = prompts[prompt_name]
        old_version = old_info.get("version", "0.0.0")
        old_schema = old_info.get("schema_version", "0.0")
        new_schema = new_schema_version or old_schema

        # 影响分析：查看哪些下游Prompt依赖此Prompt
        impact = self._analyze_upgrade_impact(prompt_name, old_schema, new_schema)

        # 更新manifest
        old_info["version"] = new_version
        old_info["schema_version"] = new_schema

        if changelog_entry:
            if "changelog" not in old_info:
                old_info["changelog"] = []
            old_info["changelog"].append({
                "version": new_version,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "change": changelog_entry,
            })

        # 更新Schema兼容性矩阵
        if breaking_changes:
            matrix = self._manifest.get("schema_compatibility_matrix", {})
            if old_schema not in matrix:
                matrix[old_schema] = {
                    "compatible_with": [],
                    "breaking_changes": [],
                    "migrate_from": {},
                }
            matrix[old_schema]["breaking_changes"].extend(breaking_changes)
            self._manifest["schema_compatibility_matrix"] = matrix

        self._save_manifest()

        # ===== Schema变更时自动标记受影响的下游引用 =====
        impact_details = impact  # 基础影响分析
        if new_schema != old_schema:
            try:
                from agent.reference_tracker import analyze_version_impact
                # Pipeline prompt名 → 阶段名的映射
                stage_name = prompt_name
                if "_P" in prompt_name:
                    stage_name = prompt_name.split("_P")[1].split("_")[0]
                    stage_name = "P" + stage_name  # e.g., "P3"

                ref_impact = analyze_version_impact(stage_name)
                impact_details = {
                    "upgraded_prompt": prompt_name,
                    "schema_changed": True,
                    "old_schema": old_schema,
                    "new_schema": new_schema,
                    "impacted_downstream": impact.get("impacted_downstream", []),
                    "stale_marked": ref_impact.get("stale_marked", 0),
                    "affected_refs": ref_impact.get("total_affected_refs", 0),
                    "warning": f"已自动标记 {ref_impact.get('stale_marked', 0)} 条引用为过期" if ref_impact.get("stale_marked", 0) > 0
                               else "Schema未变更或未找到受影响的引用",
                }
            except Exception as e:
                impact_details["reference_tracking_error"] = str(e)

        return {
            "success": True,
            "prompt": prompt_name,
            "old_version": old_version,
            "new_version": new_version,
            "old_schema": old_schema,
            "new_schema": new_schema,
            "impact": impact_details,
        }

    def _analyze_upgrade_impact(self, prompt_name: str,
                                 old_schema: str, new_schema: str) -> dict:
        """分析升级影响范围"""
        refs = self._manifest.get("cross_prompt_references", {})
        referenced_by = refs.get(prompt_name, {}).get("referenced_by", [])

        impacted = []
        for dep_prompt in referenced_by:
            dep_info = self._manifest.get("prompts", {}).get(dep_prompt, {})
            impacted.append({
                "prompt": dep_prompt,
                "version": dep_info.get("version", "unknown"),
                "schema_version": dep_info.get("schema_version", "unknown"),
                "needs_migration": old_schema != new_schema,
            })

        return {
            "upgraded_prompt": prompt_name,
            "schema_changed": old_schema != new_schema,
            "impacted_downstream": impacted,
            "warning": "Schema变更！下游Prompt可能需要迁移" if old_schema != new_schema else "Schema未变更，下游无需迁移",
        }

    # ========== 输出版戳 ==========

    def generate_output_stamp(self, prompt_name: str, element_id: str) -> dict:
        """
        生成输出数据的版本印戳
        每条Agent输出都应携带此印戳，保证历史数据可解释
        """
        info = self._manifest.get("prompts", {}).get(prompt_name, {})
        return {
            "prompt_version": info.get("version", "unknown"),
            "schema_version": info.get("schema_version", "unknown"),
            "prompt_name": prompt_name,
            "element_id": element_id,
            "timestamp": datetime.now().isoformat(),
            "active_manifest_version": self._manifest.get("version", "unknown"),
        }

    # ========== 统计 ==========

    def get_summary(self) -> dict:
        """获取版本管理概览"""
        prompts = self._manifest.get("prompts", {})
        matrix = self._manifest.get("schema_compatibility_matrix", {})

        return {
            "manifest_version": self._manifest.get("version"),
            "active_version_dir": self.active_version,
            "total_prompts": len(prompts),
            "prompt_versions": {
                name: info.get("version", "unknown")
                for name, info in prompts.items()
            },
            "schema_versions": list(set(
                info.get("schema_version", "unknown")
                for info in prompts.values()
            )),
            "compatibility_entries": len(matrix),
            "last_updated": self._manifest.get("last_updated"),
        }


# ========== 全局单例 ==========

_pm_instance: Optional[PromptManager] = None

def get_prompt_manager() -> PromptManager:
    """获取全局PromptManager单例"""
    global _pm_instance
    if _pm_instance is None:
        _pm_instance = PromptManager()
    return _pm_instance
