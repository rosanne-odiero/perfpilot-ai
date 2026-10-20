from app.db import Session


def seed(db: Session, ids: list[int]) -> None:
    for item_id in ids:
        db.query(item_id).first()


if __name__ == "__main__":
    seed(Session(), [1, 2, 3])
