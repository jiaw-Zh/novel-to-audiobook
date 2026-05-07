"""
角色-音色映射管理
支持三种模式：voicedesign / preset / clone

音色分配策略：
  - 使用预置音色做性别区分（男/女不同音色）
  - 使用 style_instruction 控制角色个性（年龄、情感、说话习惯）
  - 同性别角色轮换不同音色，避免重复
"""
import yaml
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class VoiceProfile:
    """一个角色的音色配置"""
    name: str                                       # 角色名
    mode: str = "preset"                            # preset / voicedesign / clone
    voice_id: str = ""                              # 预置音色 ID
    voice_prompt: str = ""                          # 音色描述（voicedesign 模式用）
    clone_audio_path: str = ""                      # 克隆音频路径
    style_instruction: str = ""                     # 风格指令（情感/语速/个性）
    gender: str = ""                                # male / female
    age: str = ""                                   # young / middle / old


# ── 预置音色池（按性别分组）──────────────────────────────────

MALE_VOICES = ["苏打", "白桦"]       # 中文男声
FEMALE_VOICES = ["冰糖", "茉莉"]     # 中文女声

# 性别 → style_instruction 模板
GENDER_STYLE = {
    "male": {
        "young": "年轻男性，声音清亮有力",
        "middle": "中年男性，声音沉稳浑厚",
        "old": "老年男性，声音苍老低沉",
    },
    "female": {
        "young": "年轻女性，声线清甜明亮",
        "middle": "中年女性，声音温柔醇厚",
        "old": "老年女性，声音温和慈祥",
    },
}


class VoiceManager:
    """角色音色管理器"""

    def __init__(self, voices_path: Optional[str] = None):
        self._profiles: dict[str, VoiceProfile] = {}
        self._default: VoiceProfile = VoiceProfile(
            name="旁白",
            mode="preset",
            voice_id="白桦",
            style_instruction="用标准的播音腔朗读，语速适中，情感克制",
        )
        # 计数器：同性别角色轮换音色
        self._male_idx = 0
        self._female_idx = 0

        if voices_path and Path(voices_path).exists():
            self._load_from_yaml(voices_path)

    def _load_from_yaml(self, path: str):
        """从 YAML 文件加载角色配置"""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            return

        # 默认配置
        if "default" in data:
            d = data["default"]
            self._default = VoiceProfile(
                name="默认",
                mode=d.get("mode", "preset"),
                voice_id=d.get("voice_id", "白桦"),
                voice_prompt=d.get("voice_prompt", ""),
                clone_audio_path=d.get("clone_audio_path", ""),
                style_instruction=d.get("style_instruction", ""),
            )

        # 角色配置
        for name, cfg in data.get("characters", {}).items():
            self._profiles[name] = VoiceProfile(
                name=name,
                mode=cfg.get("mode", "preset"),
                voice_id=cfg.get("voice_id", ""),
                voice_prompt=cfg.get("voice_prompt", ""),
                clone_audio_path=cfg.get("clone_audio_path", ""),
                style_instruction=cfg.get("style_instruction", ""),
                gender=cfg.get("gender", ""),
                age=cfg.get("age", ""),
            )

        logger.info(f"加载了 {len(self._profiles)} 个角色的音色配置")

    def get_profile(self, speaker: str) -> VoiceProfile:
        """获取角色的音色配置，不存在则自动推断"""
        if speaker in self._profiles:
            return self._profiles[speaker]

        if speaker == "旁白":
            return self._default

        # 自动推断
        profile = self._infer_profile(speaker)
        self._profiles[speaker] = profile
        return profile

    def _pick_voice(self, gender: str) -> str:
        """轮换选择预置音色"""
        if gender == "female":
            voice = FEMALE_VOICES[self._female_idx % len(FEMALE_VOICES)]
            self._female_idx += 1
            return voice
        else:
            voice = MALE_VOICES[self._male_idx % len(MALE_VOICES)]
            self._male_idx += 1
            return voice

    def _infer_profile(self, name: str) -> VoiceProfile:
        """根据角色名自动推断音色配置"""
        gender = self._infer_gender(name)
        age = self._infer_age(name)

        # 选择预置音色（轮换）
        voice_id = self._pick_voice(gender)

        # 构建 style_instruction（控制角色个性）
        style_parts = []
        base_style = GENDER_STYLE.get(gender, {}).get(age, "")
        if base_style:
            style_parts.append(base_style)

        return VoiceProfile(
            name=name,
            mode="preset",
            voice_id=voice_id,
            style_instruction="，".join(style_parts) if style_parts else "",
            gender=gender,
            age=age,
        )

    def _infer_gender(self, name: str) -> str:
        """推断性别"""
        female_chars = set("芳娜敏静丽莉雪梅琳玲琴瑶璇颖婷芸菲薇蕾璐妍姝婕嫣婉娴碧翠鸳鸯奴婢妃琬秋月霜烟霞莺燕芹黛容")
        if any(c in female_chars for c in name):
            return "female"
        return "male"

    def _infer_age(self, name: str) -> str:
        """推断年龄"""
        old_chars = set("老翁伯公爷太师祖")
        if any(c in old_chars for c in name):
            return "old"
        middle_chars = set("叔婶")
        if any(c in middle_chars for c in name):
            return "middle"
        young_chars = set("小少童")
        if any(c in young_chars for c in name):
            return "young"
        return "young"

    def auto_assign_voices(self, speakers: set[str]) -> dict[str, VoiceProfile]:
        """为所有角色自动分配音色"""
        result = {}
        for speaker in speakers:
            result[speaker] = self.get_profile(speaker)
        return result

    def merge_voice_hint(self, speaker: str, voice_hint: str):
        """合并 LLM 提供的 voice_hint 到角色配置"""
        if speaker not in self._profiles:
            return
        profile = self._profiles[speaker]
        if voice_hint and voice_hint not in profile.style_instruction:
            if profile.style_instruction:
                profile.style_instruction += "，" + voice_hint
            else:
                profile.style_instruction = voice_hint

    def export_yaml(self, path: str):
        """导出当前角色配置到 YAML"""
        data = {"default": {}, "characters": {}}

        data["default"] = {
            "mode": self._default.mode,
            "voice_id": self._default.voice_id,
            "style_instruction": self._default.style_instruction,
        }

        for name, profile in self._profiles.items():
            entry = {
                "mode": profile.mode,
                "voice_id": profile.voice_id,
                "gender": profile.gender,
                "age": profile.age,
            }
            if profile.style_instruction:
                entry["style_instruction"] = profile.style_instruction
            if profile.voice_prompt:
                entry["voice_prompt"] = profile.voice_prompt
            data["characters"][name] = entry

        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

        logger.info(f"角色配置已导出到 {path}")
