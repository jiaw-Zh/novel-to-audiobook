"""
角色-音色映射管理
支持三种模式：voicedesign / preset / clone
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
    mode: str = "voicedesign"                       # preset / voicedesign / clone
    voice_id: str = ""                              # 预置音色 ID
    voice_prompt: str = ""                          # 音色描述
    clone_audio_path: str = ""                      # 克隆音频路径
    style_instruction: str = ""                     # 默认风格指令
    gender: str = ""                                # male / female
    age: str = ""                                   # young / middle / old


# ── 默认音色模板 ──────────────────────────────────────────────

VOICE_TEMPLATES = {
    "male_young": VoiceProfile(
        name="",
        mode="voicedesign",
        voice_prompt="年轻男性，20岁出头，声音清亮有力，略带青涩，充满朝气",
        gender="male",
        age="young",
    ),
    "male_middle": VoiceProfile(
        name="",
        mode="voicedesign",
        voice_prompt="中年男性，35岁左右，声音沉稳浑厚，有磁性，说话不紧不慢",
        gender="male",
        age="middle",
    ),
    "male_old": VoiceProfile(
        name="",
        mode="voicedesign",
        voice_prompt="老年男性，70多岁，声音苍老沙哑，语速缓慢，带着岁月的沧桑感",
        gender="male",
        age="old",
    ),
    "female_young": VoiceProfile(
        name="",
        mode="voicedesign",
        voice_prompt="年轻女性，20岁左右，声线清甜明亮，轻声细语，如春风拂面",
        gender="female",
        age="young",
    ),
    "female_middle": VoiceProfile(
        name="",
        mode="voicedesign",
        voice_prompt="中年女性，35岁左右，声音温柔醇厚，端庄大方，说话有条不紊",
        gender="female",
        age="middle",
    ),
    "female_old": VoiceProfile(
        name="",
        mode="voicedesign",
        voice_prompt="老年女性，60多岁，声音温和慈祥，语速偏慢，带着慈母般的关怀",
        gender="female",
        age="old",
    ),
    "narrator": VoiceProfile(
        name="旁白",
        mode="preset",
        voice_id="白桦",
        voice_prompt="标准播音腔男性，语速适中，声音清晰沉稳，情感克制",
        style_instruction="用标准的播音腔朗读，语速适中，情感克制",
    ),
}


class VoiceManager:
    """角色音色管理器"""

    def __init__(self, voices_path: Optional[str] = None):
        self._profiles: dict[str, VoiceProfile] = {}
        self._default: VoiceProfile = VOICE_TEMPLATES["narrator"]

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
                mode=d.get("mode", "voicedesign"),
                voice_id=d.get("voice_id", ""),
                voice_prompt=d.get("voice_prompt", ""),
                clone_audio_path=d.get("clone_audio_path", ""),
                style_instruction=d.get("style_instruction", ""),
            )

        # 角色配置
        for name, cfg in data.get("characters", {}).items():
            self._profiles[name] = VoiceProfile(
                name=name,
                mode=cfg.get("mode", "voicedesign"),
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

        # 根据角色名推断性别和年龄
        profile = self._infer_profile(speaker)
        self._profiles[speaker] = profile
        logger.info(f"自动推断角色 '{speaker}' 的音色: {profile.voice_prompt}")
        return profile

    def _infer_profile(self, name: str) -> VoiceProfile:
        """根据角色名自动推断音色配置"""
        # 简单的性别/年龄推断规则
        gender = "male"
        age = "young"

        # 女性名字特征
        female_chars = set("芳娜敏静丽莉雪梅琳玲琴瑶璇颖婷芸菲薇蕾璇璐妍姝婕")
        if any(c in female_chars for c in name):
            gender = "female"

        # 老年角色特征
        old_chars = set("老翁伯公爷太师祖")
        if any(c in old_chars for c in name):
            age = "old"
        # 中年特征
        elif any(c in set("叔婶") for c in name):
            age = "middle"
        # 幼年特征
        elif any(c in set("小少童") for c in name):
            age = "young"

        template_key = f"{gender}_{age}"
        template = VOICE_TEMPLATES.get(template_key, VOICE_TEMPLATES["male_young"])

        return VoiceProfile(
            name=name,
            mode="voicedesign",
            voice_prompt=template.voice_prompt,
            gender=gender,
            age=age,
        )

    def auto_assign_voices(self, speakers: set[str]) -> dict[str, VoiceProfile]:
        """为所有角色自动分配音色"""
        result = {}
        for speaker in speakers:
            result[speaker] = self.get_profile(speaker)
        return result

    def export_yaml(self, path: str):
        """导出当前角色配置到 YAML"""
        data = {"default": {}, "characters": {}}

        data["default"] = {
            "mode": self._default.mode,
            "voice_id": self._default.voice_id,
            "voice_prompt": self._default.voice_prompt,
        }

        for name, profile in self._profiles.items():
            data["characters"][name] = {
                "mode": profile.mode,
                "voice_id": profile.voice_id,
                "voice_prompt": profile.voice_prompt,
                "gender": profile.gender,
                "age": profile.age,
            }

        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

        logger.info(f"角色配置已导出到 {path}")
