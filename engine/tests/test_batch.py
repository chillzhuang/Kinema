# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""kinema.batch 单元测试：四操作、锁定保护、自动 retake、undo 还原、日志上限。"""
from __future__ import annotations

import unittest

from kinema import batch, review
from kinema.errors import KinemaError
from tests.support import FakeProject, fake_path


def _project(shots):
    return FakeProject(fake_path("unused"), {"shots": shots})


class TestFieldStageMapping(unittest.TestCase):
    """「改这个字段会让哪些阶段的产物失效」只有 `review.STAGE_FIELDS` 一处真源。"""

    def test_undo_leaves_locked_shots_alone(self):
        """批量之后人已定稿的镜不因撤销回旧值，锁只能由人解。"""
        p = _project([{"id": 1, "image_prompt": "旧"}, {"id": 2, "image_prompt": "旧"}])
        r = batch.apply(p, p.data["shots"], "image_prompt", "set", "新")
        review.set_state(p.data["shots"][0], "image", "done")
        u = batch.undo(p, r["op_id"])
        self.assertEqual((u["restored"], u["skipped_locked"]), (1, 1))
        self.assertEqual(p.data["shots"][0]["image_prompt"], "新")
        self.assertEqual(p.data["shots"][0]["review"]["image"]["state"], "done")
        self.assertEqual(p.data["shots"][1]["image_prompt"], "旧")

    def test_mapping_and_ops(self):
        self.assertEqual(review.stages_for("image_prompt"), ("image",))
        self.assertEqual(review.stages_for("video_prompt"), ("clip",))
        self.assertEqual(review.stages_for("caption"), ())    # 仅文本，不触发重生
        self.assertEqual(review.stages_for("transition"), ())
        self.assertEqual(batch.OPS, ("set", "append", "prepend", "replace"))

    def test_editable_fields_are_declared_in_the_single_source(self):
        """批量可编辑字段是 `STAGE_FIELDS` 的子集，且声明的阶段都是合法阶段。

        漏登记的字段在 `apply` 里静默拿到空阶段元组：字段改了、产物不重生、
        也不报错。"""
        for f in batch.EDITABLE_FIELDS:
            self.assertIn(f, review.STAGE_FIELDS, f)
        for f, stages in review.STAGE_FIELDS.items():
            for st in stages:
                self.assertIn(st, review.STAGES, f"{f} → {st}")

    def test_fields_for_is_the_exact_inverse(self):
        for st in review.STAGES:
            self.assertEqual(
                review.fields_for(st),
                frozenset(f for f, ss in review.STAGE_FIELDS.items() if st in ss))

    def test_video_delta_fields_are_clip_stage(self):
        """delta 骨架四字段必须能被 batch edit 批量改，且属 clip 阶段
        ——它们会被 prompts.video_prompt 拼进视频提示词，改了就得重生片段。
        漏登记的话 `batch edit --field action` 会直接抛「不支持批量编辑的字段」，
        与「全片级修改一律走 batch edit、禁止逐镜手改 JSON」的纪律正面冲突。

        `entry_state` 若只在 Gateway 一侧登记、批量编辑这边漏掉，
        承接契约的两半里就只有 `end_state` 改得动。"""
        for f in ("action", "entry_state", "end_state", "light_shift"):
            self.assertEqual(review.stages_for(f), ("clip",), f)
            self.assertIn(f, batch.EDITABLE_FIELDS, f)

    def test_fields_that_feed_two_stages_invalidate_both(self):
        """两侧都吃的字段不能只标一个阶段。

        `negative_prompt` 同时拼进生图与视频提示词；`narration` 除了决定 TTS，
        native 还把它写进视频提示词、dubbed 的 ref_audio 由它合成。只标一边时，
        已生成的另一侧产物既不重做也不告警。"""
        self.assertEqual(review.stages_for("negative_prompt"), ("image", "clip"))
        self.assertEqual(review.stages_for("narration"), ("audio", "clip"))
        shots = [{"id": "1", "narration": "旧旁白"}]
        proj = _project(shots)
        res = batch.apply(proj, shots, "narration", "set", "新旁白")
        self.assertEqual(res["stages"], ("audio", "clip"))
        self.assertEqual(review.get_state(shots[0], "audio"), "retake")
        self.assertEqual(review.get_state(shots[0], "clip"), "retake")

    def test_delta_field_edit_marks_clip_retake(self):
        shots = [{"id": "1", "action": "抬手"}]
        proj = _project(shots)
        res = batch.apply(proj, shots, "action", "set", "抬手抹掉眼角")
        self.assertEqual(res["stages"], ("clip",))
        self.assertEqual(shots[0]["action"], "抬手抹掉眼角")
        self.assertEqual(review.get_state(shots[0], "clip"), "retake")


class TestApply(unittest.TestCase):
    def test_set_marks_retake_and_logs(self):
        shots = [{"id": "1", "image_prompt": "白天的街道"},
                 {"id": "2", "image_prompt": "白天的公园"}]
        proj = _project(shots)
        res = batch.apply(proj, shots, "image_prompt", "set", "夜晚的街道")
        self.assertEqual(res["changed"], 2)
        self.assertEqual(res["stages"], ("image",))
        self.assertIsNotNone(res["op_id"])
        self.assertEqual(shots[0]["image_prompt"], "夜晚的街道")
        # 提示词类字段 → 所属阶段自动置 retake，意见记录批量说明
        self.assertEqual(review.get_state(shots[0], "image"), "retake")
        self.assertIn("image_prompt", review.get_note(shots[0], "image"))
        self.assertEqual(len(proj.data["batch_ops"]), 1)
        self.assertEqual(proj.saved, 1)

    def test_append_and_prepend(self):
        shots = [{"id": "1", "image_prompt": "街道"}]
        proj = _project(shots)
        batch.apply(proj, shots, "image_prompt", "append", "，霓虹闪烁")
        self.assertEqual(shots[0]["image_prompt"], "街道，霓虹闪烁")
        batch.apply(proj, shots, "image_prompt", "prepend", "雨夜，")
        self.assertEqual(shots[0]["image_prompt"], "雨夜，街道，霓虹闪烁")

    def test_replace_and_unchanged(self):
        shots = [{"id": "1", "image_prompt": "白天的街道"},
                 {"id": "2", "image_prompt": "夜晚的公园"}]     # 无"白天" → 不变
        proj = _project(shots)
        res = batch.apply(proj, shots, "image_prompt", "replace", "白天=>夜晚")
        self.assertEqual(res["changed"], 1)
        self.assertEqual(res["unchanged"], 1)
        self.assertEqual(shots[0]["image_prompt"], "夜晚的街道")
        # 无镜可改时不产生日志条目
        res2 = batch.apply(proj, shots, "image_prompt", "replace", "不存在=>x")
        self.assertIsNone(res2["op_id"])
        self.assertEqual(res2["unchanged"], 2)

    def test_replace_bad_format_raises(self):
        shots = [{"id": "1", "image_prompt": "白天"}]
        with self.assertRaises(KinemaError):
            batch.apply(_project(shots), shots, "image_prompt", "replace", "白天夜晚")

    def test_unknown_field_raises(self):
        with self.assertRaises(KinemaError):
            batch.apply(_project([]), [], "bgm_prompt", "set", "x")

    def test_locked_shot_skipped_by_default(self):
        shots = [{"id": "1", "image_prompt": "白天"}]
        review.set_state(shots[0], "image", "done")           # 定稿锁定
        proj = _project(shots)
        res = batch.apply(proj, shots, "image_prompt", "set", "夜晚")
        self.assertEqual(res["skipped_locked"], 1)
        self.assertEqual(res["changed"], 0)
        self.assertEqual(shots[0]["image_prompt"], "白天")     # 字段未动

    def test_include_locked_changes_field_but_keeps_done(self):
        shots = [{"id": "1", "image_prompt": "白天"}]
        review.set_state(shots[0], "image", "done")
        proj = _project(shots)
        res = batch.apply(proj, shots, "image_prompt", "set", "夜晚",
                          include_locked=True)
        self.assertEqual(res["changed"], 1)
        self.assertEqual(shots[0]["image_prompt"], "夜晚")
        # 锁定镜纳入编辑，但 done 状态不被打回（防烧钱语义不破）
        self.assertEqual(review.get_state(shots[0], "image"), "done")
        self.assertEqual(res["retaken"], [])

    def test_caption_and_no_retake(self):
        shots = [{"id": "1", "caption": "旧字幕", "narration": "旧旁白"}]
        proj = _project(shots)
        batch.apply(proj, shots, "caption", "set", "新字幕")   # 无受影响阶段的字段
        self.assertEqual(shots[0].get("review"), None)         # 不触发任何审阅状态
        batch.apply(proj, shots, "narration", "set", "新旁白", mark_retake=False)
        self.assertEqual(review.get_state(shots[0], "audio"), "todo")   # --no-retake
        self.assertEqual(review.get_state(shots[0], "clip"), "todo")


class TestUndo(unittest.TestCase):
    def test_undo_restores_field_and_marks_retake(self):
        """撤销是一次编辑：字段回旧值，受影响阶段与 apply 同规则置 retake——批量之后
        按新值重生过的产物与旧值一样对不上，回写批量前的审阅快照会把新图标成待审。"""
        shots = [{"id": "1", "narration": "旧文案"}]
        review.set_state(shots[0], "audio", "wfa")
        proj = _project(shots)
        r = batch.apply(proj, shots, "narration", "set", "新文案")
        self.assertEqual(review.get_state(shots[0], "audio"), "retake")
        review.set_state(shots[0], "audio", "wfa")           # 已按新词重配音、待审
        out = batch.undo(proj)
        self.assertEqual(out["restored"], 1)
        self.assertEqual(shots[0]["narration"], "旧文案")
        for st in ("audio", "clip"):
            self.assertEqual(review.get_state(shots[0], st), "retake")
        self.assertIn(r["op_id"], review.get_note(shots[0], "audio"))
        self.assertEqual(proj.data["batch_ops"], [])

    def test_undo_honors_no_retake_choice(self):
        shots = [{"id": "1", "image_prompt": "旧"}]
        review.set_state(shots[0], "image", "wfa")
        proj = _project(shots)
        batch.apply(proj, shots, "image_prompt", "set", "新", mark_retake=False)
        batch.undo(proj)
        self.assertEqual(shots[0]["image_prompt"], "旧")
        self.assertEqual(review.get_state(shots[0], "image"), "wfa")

    def test_undo_by_op_id(self):
        shots = [{"id": "1", "narration": "旁白A", "caption": "字幕A"}]
        proj = _project(shots)
        r1 = batch.apply(proj, shots, "narration", "set", "旁白B")
        batch.apply(proj, shots, "caption", "set", "字幕B")
        batch.undo(proj, r1["op_id"])                          # 只撤销第一笔
        self.assertEqual(shots[0]["narration"], "旁白A")
        self.assertEqual(shots[0]["caption"], "字幕B")
        self.assertEqual(len(proj.data["batch_ops"]), 1)

    def test_undo_errors(self):
        proj = _project([])
        with self.assertRaises(KinemaError):
            batch.undo(proj)                                   # 日志为空
        shots = [{"id": "1", "narration": "a"}]
        proj = _project(shots)
        batch.apply(proj, shots, "narration", "set", "b")
        with self.assertRaises(KinemaError):
            batch.undo(proj, "no-such-op")

    def test_log_capped_at_50(self):
        shots = [{"id": "1", "narration": "旧"}]
        proj = _project(shots)
        proj.data["batch_ops"] = [{"id": str(i)} for i in range(55)]
        res = batch.apply(proj, shots, "narration", "set", "新")
        log = proj.data["batch_ops"]
        self.assertEqual(len(log), 50)                         # 只留最近 50 条
        self.assertEqual(log[-1]["id"], res["op_id"])          # 新条目在最后
        self.assertEqual(log[0]["id"], "6")                    # 最旧的被裁掉


if __name__ == "__main__":
    unittest.main()
