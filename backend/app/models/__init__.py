"""SQLAlchemy models.

Importing this package registers every table on `Base.metadata`, which Alembic
autogenerate and `create_all` both rely on.
"""

from app.models.base import Base, TimestampMixin, utcnow
from app.models.content import (
    CurriculumStandard,
    ExaminerFeedback,
    Figure,
    Image,
    ModelAnswerPoint,
    OsceFigure,
    OsceStation,
    Question,
    QuestionPart,
    SourceDocument,
)
from app.models.exam import (
    Answer,
    ExamPaper,
    ExamPaperQuestion,
    ExamSession,
    Grade,
    SessionResult,
)
from app.models.ops import AiCall, ErrorLog, Job
from app.models.osce import (
    AudioClip,
    OsceCircuit,
    OsceGrade,
    OsceResponse,
    OsceResult,
    OsceSession,
)
from app.models.user import Invite, Setting, User

__all__ = [
    "Base",
    "TimestampMixin",
    "utcnow",
    "User",
    "Invite",
    "Setting",
    "CurriculumStandard",
    "SourceDocument",
    "Image",
    "Question",
    "QuestionPart",
    "ModelAnswerPoint",
    "ExaminerFeedback",
    "Figure",
    "OsceStation",
    "OsceFigure",
    "ExamPaper",
    "ExamPaperQuestion",
    "ExamSession",
    "Answer",
    "Grade",
    "SessionResult",
    "Job",
    "AiCall",
    "ErrorLog",
    "AudioClip",
    "OsceCircuit",
    "OsceSession",
    "OsceResponse",
    "OsceGrade",
    "OsceResult",
]
