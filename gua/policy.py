"""信息 / 能力策略（审查条目 6）：一次性从配置推导出“这次运行允许各组件看到什么、调用什么”，
所有组件（planner / actor 提示词 / grounder / verifier / reflector / recovery / 收尾核验）都只读这一个对象，
避免 v0.2 那样“消融名字说纯视觉，但 Actor 提示词里仍然有元素 id/名字/值”的不一致。

配置键（全部可选，缺省 = 主方法）：

  observation.use_a11y        总开关：false = 纯视觉，所有组件都看不到无障碍树/DOM 文本（vision_only 消融）
  observation.a11y_in_prompts 模型提示词里是否放元素列表 / 可见文本（默认随 use_a11y）
  grounding.use_a11y          定位是否用无障碍树精确匹配
  verification.use_a11y       L1 规则是否用无障碍树证据（期望文本、输入框值、复选框状态、对话框、忙碌指示）
  verification.trigger        none | on_event | every_step
  verification.llm            是否允许 L2 模型验证（步骤级、子目标收尾、任务收尾）。false = 纯规则
  verification.verify_goals   子目标收尾核验（done 时）
  verification.final_check    任务收尾核验（默认随 verify_goals；显式 false 才关闭）
  verification.final_l2       when_needed（默认：所有子目标都有可规则核验的 expect_text 且全部通过时不再调 L2）| always
  verification.on_uncertain   收尾核验 uncertain 时：replan（默认：重规划一次后报告 uncertain）| fail
  reflection.enabled          反思器（一次模型调用）
  recovery.enabled / fixed_retry   恢复模式：classified | fixed_retry | none

不受本策略约束的部分（有意为之，见 README）：
- 安全闸门始终使用无障碍信息（元素名、is_password）判断危险动作——消融不能削弱安全性；
- 窗口标题 / URL 属于窗口管理器元数据（焦点检查需要），vision_only 下仍提供给模型。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilityPolicy:
    a11y_grounding: bool = True
    a11y_in_prompts: bool = True
    a11y_rules: bool = True
    window_metadata: bool = True
    step_trigger: str = "on_event"
    llm_step_verify: bool = True
    llm_goal_verify: bool = True
    llm_reflection: bool = True
    goal_check: bool = True
    final_check: bool = True
    final_l2: str = "when_needed"
    on_uncertain: str = "replan"
    recovery: str = "classified"

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "CapabilityPolicy":
        o = cfg.get("observation") or {}
        g = cfg.get("grounding") or {}
        v = cfg.get("verification") or {}
        r = cfg.get("recovery") or {}
        rf = cfg.get("reflection") or {}
        master = bool(o.get("use_a11y", True))
        trigger = v.get("trigger", "on_event")
        llm_ok = bool(v.get("llm", True)) and trigger != "none"
        goal = bool(v.get("verify_goals", True))
        final = v.get("final_check")
        final = goal if final is None else bool(final)
        if not r.get("enabled", True):
            rec = "none"
        elif r.get("fixed_retry", False):
            rec = "fixed_retry"
        else:
            rec = "classified"
        return cls(
            a11y_grounding=master and bool(g.get("use_a11y", g.get("use_uia", True))),
            a11y_in_prompts=master and bool(o.get("a11y_in_prompts", True)),
            a11y_rules=master and bool(v.get("use_a11y", True)),
            window_metadata=bool(o.get("window_metadata", True)),
            step_trigger=trigger,
            llm_step_verify=llm_ok,
            llm_goal_verify=llm_ok,
            llm_reflection=bool(rf.get("enabled", True)),
            goal_check=goal,
            final_check=final and trigger != "none",
            final_l2=v.get("final_l2", "when_needed"),
            on_uncertain=v.get("on_uncertain", "replan"),
            recovery=rec,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.to_dict().items())


DEFAULT_POLICY = CapabilityPolicy()
