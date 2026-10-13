"""
单元测试 - cipher.py 加解密与序列号测试
"""

import unittest
from server.protocol.cipher import (
    DefaultMsgSeq,
    c_rem,
    do_bytes,
    to_int8,
    to_int32,
    undo_bytes,
)


class TestCipherModule(unittest.TestCase):

    def test_int_helpers(self):
        """测试 32 位与 8 位溢出转换辅助函数"""
        self.assertEqual(to_int32(0), 0)
        self.assertEqual(to_int32(0xFFFFFFFF), -1)
        self.assertEqual(to_int32(0x7FFFFFFF), 2147483647)
        self.assertEqual(to_int32(0x80000000), -2147483648)

        self.assertEqual(to_int8(0), 0)
        self.assertEqual(to_int8(0xFF), -1)
        self.assertEqual(to_int8(0x7F), 127)
        self.assertEqual(to_int8(0x80), -128)

    def test_c_rem(self):
        """测试 C 语言截断取整求余"""
        self.assertEqual(c_rem(5, 4), 1)
        self.assertEqual(c_rem(-5, 4), -1)
        self.assertEqual(c_rem(7, 4), 3)
        self.assertEqual(c_rem(-7, 4), -3)
        self.assertEqual(c_rem(0, 4), 0)
        self.assertEqual(c_rem(4, 4), 0)

    def test_default_msg_seq(self):
        """测试 DefaultMsgSeq 包序号发生器"""
        seq_gen = DefaultMsgSeq()
        self.assertEqual(seq_gen.current, -1)
        # 首包返回 0
        self.assertEqual(seq_gen.next_seq(), 0)
        self.assertEqual(seq_gen.current, 0)
        # 第 2 包: 0 * 2 + 1 = 1
        self.assertEqual(seq_gen.next_seq(), 1)
        # 第 3 包: 1 * 2 + 1 = 3
        self.assertEqual(seq_gen.next_seq(), 3)
        # 第 4 包: 3 * 2 + 1 = 7
        self.assertEqual(seq_gen.next_seq(), 7)
        # 第 5 包: 7 * 2 + 1 = 15
        self.assertEqual(seq_gen.next_seq(), 15)

        # 重置测试
        seq_gen.reset()
        self.assertEqual(seq_gen.current, -1)
        self.assertEqual(seq_gen.next_seq(), 0)

    def test_do_bytes_char_id_zero_roundtrip(self):
        """测试 char_id == 0 时的加密与自逆解密"""
        test_lengths = [0, 1, 2, 3, 4, 7, 8, 15, 16, 64, 255]
        test_seqs = [0, 1, 3, 7, 15, 100, 1234567, 2147483647]

        for seq in test_seqs:
            for length in test_lengths:
                original = bytes(range(length))
                # 加密
                encrypted = do_bytes(seq, original, char_id=0)
                # 自逆解密 (do_bytes 自身即可解密)
                decrypted_self = do_bytes(seq, encrypted, char_id=0)
                self.assertEqual(original, decrypted_self, f"Failed at seq={seq}, len={length} (self)")
                # undo_bytes 解密
                decrypted_undo = undo_bytes(seq, encrypted, char_id=0)
                self.assertEqual(original, decrypted_undo, f"Failed at seq={seq}, len={length} (undo)")

    def test_do_bytes_char_id_nonzero_roundtrip(self):
        """测试 char_id != 0 时的复合置换加密与逆向解密"""
        test_lengths = [0, 1, 2, 3, 4, 5, 7, 8, 12, 16, 33, 100, 512]
        test_seqs = [0, 1, 2, 3, 7, 42, 99999, 10000000]
        char_id = 10001

        for seq in test_seqs:
            for length in test_lengths:
                original = bytes([i % 256 for i in range(length)])
                encrypted = do_bytes(seq, original, char_id=char_id)
                # 如果长度 > 0 且 seq != 0，密文通常不等于明文
                if length >= 4 and seq != 0:
                    self.assertNotEqual(original, encrypted, f"Ciphertext should differ at seq={seq}, len={length}")

                # 逆向解密恢复
                decrypted = undo_bytes(seq, encrypted, char_id=char_id)
                self.assertEqual(original, decrypted, f"Roundtrip failed at seq={seq}, len={length}")


if __name__ == "__main__":
    unittest.main()
