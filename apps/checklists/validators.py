from django.core.exceptions import ValidationError

MAX_DRAWING_BYTES = 20 * 1024 * 1024


def validate_pdf(upload):
    """Engineering drawings must be real PDF files (PRD 4.2), at most 20 MB."""
    if not upload.name.lower().endswith(".pdf"):
        raise ValidationError("Upload a PDF file (.pdf).")
    if upload.size > MAX_DRAWING_BYTES:
        raise ValidationError("The drawing is larger than 20 MB.")
    position = upload.tell() if hasattr(upload, "tell") else 0
    upload.seek(0)
    header = upload.read(5)
    upload.seek(position)
    if header != b"%PDF-":
        raise ValidationError("This file is not a valid PDF document.")
