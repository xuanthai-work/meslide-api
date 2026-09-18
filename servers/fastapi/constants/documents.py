PDF_EXTENSIONS = [".pdf"]
TEXT_EXTENSIONS = [".txt"]

WORD_EXTENSIONS = [".docx", ".docm", ".odt"]
POWERPOINT_EXTENSIONS = [".pptx", ".pptm", ".odp"]
SPREADSHEET_EXTENSIONS = [".xlsx", ".xlsm", ".ods", ".csv", ".tsv"]

JPEG_EXTENSIONS = [".jpg", ".jpeg"]
PNG_EXTENSIONS = [".png"]
GIF_EXTENSIONS = [".gif"]
BMP_EXTENSIONS = [".bmp"]
TIFF_EXTENSIONS = [".tiff", ".tif"]
WEBP_EXTENSIONS = [".webp"]
SVG_EXTENSIONS = [".svg"]
IMAGE_EXTENSIONS = (
    JPEG_EXTENSIONS
    + PNG_EXTENSIONS
    + GIF_EXTENSIONS
    + BMP_EXTENSIONS
    + TIFF_EXTENSIONS
    + WEBP_EXTENSIONS
)

OFFICE_EXTENSIONS = WORD_EXTENSIONS + POWERPOINT_EXTENSIONS + SPREADSHEET_EXTENSIONS

PDF_MIME_TYPES = ["application/pdf"]
TEXT_MIME_TYPES = ["text/plain", "text/markdown"]

WORD_MIME_TYPES = [
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroenabled.12",
    "application/vnd.oasis.opendocument.text",
]

POWERPOINT_MIME_TYPES = [
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12",
    "application/vnd.oasis.opendocument.presentation",
]

SPREADSHEET_MIME_TYPES = [
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.oasis.opendocument.spreadsheet",
    "text/csv",
    "application/csv",
    "text/tab-separated-values",
    "text/tsv",
]

IMAGE_MIME_TYPES = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/bmp",
    "image/tiff",
    "image/webp",
]

UPLOAD_ACCEPTED_MIME_TYPES = (
    PDF_MIME_TYPES
    + TEXT_MIME_TYPES
    + WORD_MIME_TYPES
    + POWERPOINT_MIME_TYPES
    + SPREADSHEET_MIME_TYPES
    + IMAGE_MIME_TYPES
)

UPLOAD_ACCEPTED_EXTENSIONS = (
    PDF_EXTENSIONS + TEXT_EXTENSIONS + OFFICE_EXTENSIONS + IMAGE_EXTENSIONS
)

# Includes both MIME types and extensions because some clients upload legacy
# office files with generic content-type values.
UPLOAD_ACCEPTED_FILE_TYPES = UPLOAD_ACCEPTED_MIME_TYPES + UPLOAD_ACCEPTED_EXTENSIONS

# Maximum document paths accepted in one prompt-rewrite request. Mirrors the
# upload UI's MAX_SUPPORTED_FILES so the rewrite contract can never accept a
# file set the UI would refuse to upload.
MAX_PROMPT_REWRITE_FILE_PATHS = 8

# Kept for endpoints that strictly require modern .pptx files.
PPTX_MIME_TYPES = ["application/vnd.openxmlformats-officedocument.presentationml.presentation"]

# Backward compatibility aliases used across existing modules.
POWERPOINT_TYPES = PPTX_MIME_TYPES
WORD_TYPES = WORD_MIME_TYPES
SPREADSHEET_TYPES = SPREADSHEET_MIME_TYPES
