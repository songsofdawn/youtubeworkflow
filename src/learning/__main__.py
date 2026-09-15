"""Rebuild artifacts or queue model work through the panel's GPU resource slot."""
import argparse
from pathlib import Path

from .learning_service import LearningService, enqueue_learning
from .schemas import VideoAnalysis


def main():
    parser = argparse.ArgumentParser(description="Discovery Next learning maintenance")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--rebuild", action="store_true")
    group.add_argument("--retry-pending", action="store_true")
    group.add_argument("--reanalyze", metavar="VIDEO_ID")
    group.add_argument("--schema", action="store_true")
    args = parser.parse_args()
    if args.schema:
        import json
        print(json.dumps(VideoAnalysis.model_json_schema(), ensure_ascii=False, indent=2))
        return
    service = LearningService(args.root)
    if args.rebuild:
        print(f'Rebuilt profile: {service.rebuild()["sample_size"]} samples')
    elif args.retry_pending:
        print(f'Queued {service.queue_pending()} events; keep the control panel running.')
    else:
        with service.store.connect() as db:
            row = db.execute("SELECT event_id FROM events WHERE video_id=? ORDER BY created_at LIMIT 1", (args.reanalyze,)).fetchone()
        if row is None:
            parser.error("No learning event for this video")
        enqueue_learning(args.root, row[0], force=True)
        print("Reanalysis queued; keep the control panel running.")


if __name__ == "__main__":
    main()
