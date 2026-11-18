import json
from pathlib import Path


class RecordRepository:
    """记录库：文本记录与判题案件（事件流 + 处置）分键存放。"""

    def __init__(self, path):
        self.path = Path(path)

    def read_all(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], []
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            raise ValueError("记录库格式无效")
        cases = value.get("cases", [])
        if not isinstance(cases, list):
            raise ValueError("案件库格式无效")
        return value["records"], cases

    # 兼容旧接口
    def read(self):
        records, _ = self.read_all()
        return records

    def replace_all(self, records, cases):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"records": records, "cases": cases}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)

    # 兼容旧接口
    def replace(self, records):
        _, cases = self.read_all()
        self.replace_all(records, cases)
