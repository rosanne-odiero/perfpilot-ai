"""Minimal stand-ins so the fixture parses; never executed by the audit."""


class Session:
    def query(self, model):
        return self

    def get(self, model, key):
        return None


def get_db() -> Session:
    return Session()
