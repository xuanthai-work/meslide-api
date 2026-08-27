from enum import Enum


class ImageProvider(Enum):
    GEMINI_FLASH = "gemini_flash"
    NANOBANANA_PRO = "nanobanana_pro"
    GPT_IMAGE_1_5 = "gpt-image-1.5"
    OPENAI_COMPATIBLE = "openai_compatible"