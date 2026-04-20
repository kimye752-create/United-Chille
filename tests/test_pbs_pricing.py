"""PBS 가격 파서 단위 테스트 (네트워크 없음)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.pbs_pricing import _parse_item_page  # noqa: PLC2701


_SNIPPET = """
<table id="medicine-item" summary="Item Details">
<tr>
<th>DPMQ</th>
</tr>
<tr>
<td class="align-top" rowspan="3"><span class="item-code">14328D</span></td>
<td class="align-left"><span class="form-strength">salmeterol 50 mcg</span></td>
<td class="align-top" rowspan="3">2</td>
<td class="align-top" rowspan="3">2</td>
<td class="align-top" rowspan="3">5</td>
<td class="align-top" rowspan="3">$47.78</td>
<td class="align-top" rowspan="3">$25.00</td>
<td class="align-top" rowspan="3">$25.00</td>
</tr>
</table>
"""


class TestPbsParse(unittest.TestCase):
    def test_parse_dpmq(self) -> None:
        dpmq, _drug, pack = _parse_item_page(_SNIPPET)
        self.assertAlmostEqual(dpmq, 47.78, places=2)
        self.assertIn("salmeterol", pack.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
