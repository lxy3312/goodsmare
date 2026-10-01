"""二维码：把「手机访问」的地址画成 SVG，手机一扫就打开。

只做用得上的部分：字节模式、纠错等级 M、版本 1~40 自动选。
算法照 ISO/IEC 18004 的步骤写，对照过 Nayuki 的 QR Code generator（MIT）。
"""

from __future__ import annotations

# 纠错等级 M：每个版本（下标）每块的纠错码字数、块数
_ECC_PER_BLOCK = (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
                  26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28)
_BLOCKS = (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
           17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49)
_FORMAT_M = 0   # 格式信息里 M 级的两位


def _raw_modules(ver: int) -> int:
    """去掉定位、校正、格式信息等功能图形后，能放数据的模块数。"""
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        align = ver // 7 + 2
        n -= (25 * align - 10) * align - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _BLOCKS[ver]


# —— GF(256) 上的 Reed-Solomon ——
def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


class QR:
    def __init__(self, text: str):
        data = text.encode("utf-8")
        for ver in range(1, 41):
            count_bits = 8 if ver < 10 else 16
            if 4 + count_bits + len(data) * 8 <= _data_codewords(ver) * 8:
                break
        else:
            raise ValueError("太长了，二维码放不下")
        self.version = ver
        self.size = ver * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self._fixed = [[False] * self.size for _ in range(self.size)]

        # 字节模式：模式指示 0100，长度，数据，结束符，补齐
        bits: list[int] = []

        def put(value: int, n: int) -> None:
            bits.extend((value >> i) & 1 for i in reversed(range(n)))
        put(0b0100, 4)
        put(len(data), count_bits)
        for b in data:
            put(b, 8)
        capacity = _data_codewords(ver) * 8
        put(0, min(4, capacity - len(bits)))
        put(0, -len(bits) % 8)
        pad = 0xEC
        while len(bits) < capacity:
            put(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc(codewords))
        # 八种掩码各试一遍，挑罚分最低的
        best, best_mask = None, 0
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format(mask)
            score = self._penalty()
            if best is None or score < best:
                best, best_mask = score, mask
            self._apply_mask(mask)   # 异或两次等于撤销
        self._apply_mask(best_mask)
        self._draw_format(best_mask)

    # —— 功能图形 ——
    def _set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self._fixed[y][x] = True

    def _draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):   # 定时线
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):   # 三个角上的定位图形，连同白边
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._alignment_positions()
        last = len(pos) - 1
        for i, cx in enumerate(pos):   # 校正图形，避开三个定位图形
            for j, cy in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)   # 先占位，挑完掩码再写真的
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            v = self.version << 12 | rem
            for i in range(18):
                bit = (v >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _alignment_positions(self) -> list[int]:
        if self.version == 1:
            return []
        n = self.version // 7 + 2
        step = (self.version * 8 + n * 3 + 5) // (n * 4 - 4) * 2
        return [6] + list(reversed([self.size - 7 - i * step for i in range(n - 1)]))

    def _draw_format(self, mask: int) -> None:
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        size = self.size
        for i in range(6):
            self._set(8, i, bit[i])
        self._set(8, 7, bit[6])
        self._set(8, 8, bit[7])
        self._set(7, 8, bit[8])
        for i in range(9, 15):
            self._set(14 - i, 8, bit[i])
        for i in range(8):
            self._set(size - 1 - i, 8, bit[i])
        for i in range(8, 15):
            self._set(8, size - 15 + i, bit[i])
        self._set(8, size - 8, True)   # 固定的一个黑点

    # —— 数据 ——
    def _add_ecc(self, data: list[int]) -> list[int]:
        ver = self.version
        nblocks, ecc_len = _BLOCKS[ver], _ECC_PER_BLOCK[ver]
        raw = _raw_modules(ver) // 8
        nshort = nblocks - raw % nblocks
        short_len = raw // nblocks
        divisor = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(nblocks):
            n = short_len - ecc_len + (0 if i < nshort else 1)
            block = data[k:k + n]
            k += n
            ecc = _rs_remainder(block, divisor)
            if i < nshort:
                block.append(0)
            blocks.append(block + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, block in enumerate(blocks):
                if i != short_len - ecc_len or j >= nshort:   # 短块补的那个 0 不写
                    out.append(block[i])
        return out

    def _draw_codewords(self, codewords: list[int]) -> None:
        i, total = 0, len(codewords) * 8
        right = self.size - 1
        while right >= 1:   # 从右下角开始，两列一组蛇形往上、往下
            if right == 6:
                right = 5
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = self.size - 1 - vert if upward else vert
                    if not self._fixed[y][x] and i < total:
                        self.modules[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _apply_mask(self, mask: int) -> None:
        rule = (
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        )[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self._fixed[y][x] and rule(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    # —— 罚分：连续同色、2×2 色块、像定位图形的 1:1:3:1:1、黑白比例 ——
    def _penalty(self) -> int:
        size, m = self.size, self.modules
        score = 0
        lines = [m[y] for y in range(size)] + [[m[y][x] for y in range(size)] for x in range(size)]
        for line in lines:
            run_color, run, history = False, 0, [0] * 7
            for cell in line:
                if cell == run_color:
                    run += 1
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                else:
                    self._push_run(run, history)
                    if not run_color:
                        score += self._finder_like(history) * 40
                    run_color, run = cell, 1
            if run_color:
                self._push_run(run, history)
                run = 0
            self._push_run(run + size, history)
            score += self._finder_like(history) * 40
        for y in range(size - 1):
            for x in range(size - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return score + k * 10

    def _push_run(self, run: int, history: list[int]) -> None:
        if history[0] == 0:
            run += self.size   # 行首当成接着一段白边
        history.insert(0, run)
        history.pop()

    def _finder_like(self, h: list[int]) -> int:
        n = h[1]
        core = n > 0 and h[2] == h[4] == h[5] == n and h[3] == n * 3
        return int(core and h[0] >= n * 4 and h[6] >= n) + int(core and h[6] >= n * 4 and h[0] >= n)

    # —— 输出 ——
    def svg(self, border: int = 3) -> str:
        n = self.size + border * 2
        parts = []
        for y, row in enumerate(self.modules):
            x = 0
            while x < self.size:   # 同一行连着的黑块合成一个矩形，文件小一半多
                if not row[x]:
                    x += 1
                    continue
                start = x
                while x < self.size and row[x]:
                    x += 1
                parts.append(f"M{start + border},{y + border}h{x - start}v1h-{x - start}z")
        path = "".join(parts)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
                f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def svg(text: str) -> str:
    return QR(text).svg()
