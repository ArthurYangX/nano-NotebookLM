"""Knowledge mastery tracking — spaced repetition scoring."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from nano_notebooklm import config
from nano_notebooklm.skills.base import Skill
from nano_notebooklm.types import MasteryRecord, SkillResult

logger = logging.getLogger(__name__)


class MasteryTrackerSkill(Skill):
    name = "mastery_tracker"
    description = "Track knowledge mastery and identify weak areas"

    async def execute(self, params: dict) -> SkillResult:
        """
        Params:
            course_id (str): Course identifier
            quiz_result (dict): {answers: [{question_id, student_answer, correct, concepts}]}
        """
        course_id = params.get("course_id", "")
        quiz_result = params.get("quiz_result", {})

        if not course_id:
            return SkillResult(success=False, error="No course_id provided")

        # Load existing mastery data
        mastery_path = config.ARTIFACTS_DIR / "courses" / course_id / "mastery.json"
        mastery = self._load_mastery(mastery_path)

        # Update mastery from quiz results
        answers = quiz_result.get("answers", [])
        updated_concepts = []

        for answer in answers:
            concepts = answer.get("concepts", [])
            correct = answer.get("correct", False)

            for concept_name in concepts:
                concept_key = concept_name.lower().strip()
                if concept_key not in mastery:
                    mastery[concept_key] = {
                        "concept": concept_name,
                        "score": 0.5,
                        "attempts": 0,
                        "correct_count": 0,
                        "last_tested": None,
                        "history": [],
                    }

                record = mastery[concept_key]
                old_score = record["score"]
                record["attempts"] += 1
                if correct:
                    record["correct_count"] += 1

                # Exponential moving average scoring
                alpha = 0.3  # Weight of new observation
                new_score = alpha * (1.0 if correct else 0.0) + (1 - alpha) * old_score
                record["score"] = round(new_score, 3)
                record["last_tested"] = datetime.now().isoformat()
                record["history"].append({
                    "correct": correct,
                    "timestamp": datetime.now().isoformat(),
                    "question": answer.get("question_id", ""),
                })

                updated_concepts.append({
                    "concept": concept_name,
                    "old_score": old_score,
                    "new_score": record["score"],
                })

        # Save updated mastery
        self._save_mastery(mastery_path, mastery)

        # Identify weak areas (score < 0.5)
        weak_areas = [
            {"concept": v["concept"], "score": v["score"], "attempts": v["attempts"]}
            for v in mastery.values()
            if v["score"] < 0.5 and v["attempts"] > 0
        ]
        weak_areas.sort(key=lambda x: x["score"])

        return SkillResult(
            success=True,
            data={
                "updated_concepts": updated_concepts,
                "weak_areas": weak_areas,
                "total_concepts_tracked": len(mastery),
                "average_mastery": round(
                    sum(v["score"] for v in mastery.values()) / max(len(mastery), 1), 3
                ),
            },
        )

    def _load_mastery(self, path: Path) -> dict:
        if path.exists():
            return json.loads(path.read_text())
        return {}

    def _save_mastery(self, path: Path, mastery: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mastery, ensure_ascii=False, indent=2))
