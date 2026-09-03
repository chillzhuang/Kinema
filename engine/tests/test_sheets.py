"""设定图提示词契约（`kinema.sheets`）与版式蓝图的对齐守卫。

分工：本文件守**契约正文本身**——角色三区两视表、道具结构三视式、纯图片纪律，
以及蓝图与契约之间那几条会静默漂移的对应关系。样板怎么分发（哪些 kind 有样板、
随包分发、职责声明只在真附样板时出现、`cmd_gen_refs` 接线）在
`test_adapt.TestSheetTemplates`。
"""
import re
import struct
import unittest

from kinema import sheets


def _png_size(path):
    """PNG 画布尺寸（只读 IHDR，避免为守卫引入图像依赖）。"""
    with path.open("rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise AssertionError(f"{path.name} 不是 PNG")
    return struct.unpack(">II", head[16:24])


class BlueprintAlignmentTests(unittest.TestCase):
    """蓝图与契约必须同源——蓝图停在旧版式，等于每次生成都在教一个过时的骨架。"""

    def test_blueprint_canvas_matches_the_kind_aspect(self):
        """蓝图画布比例必须与该类设定图的出图比例同源（`aspect_for`）。

        蓝图教的是**分区的位置与比例关系**：16:9 的三区表压进 1:1 的画布，区宽与
        立像高度的比例关系全变，模型学走的就是错的骨架。钉比例而不是钉像素——
        重做蓝图时换更高分辨率是常规操作，不该惊动守卫。
        """
        for kind in ("character", "prop"):
            w, h = (int(x) for x in sheets.aspect_for(kind).split(":"))
            want = w / h
            paths = sheets.templates_for(kind)
            self.assertTrue(paths, f"{kind} 一张蓝图都不在盘")
            for path in paths:
                pw, ph = _png_size(path)
                self.assertAlmostEqual(pw / ph, want, delta=want * 0.01,
                                       msg=f"{path.name} 画布比例偏离 {kind} 的 {w}:{h}")

    def test_prop_blueprints_are_one_per_declared_layout(self):
        """道具是**一式一张样板**：`PROP_LAYOUTS` 有几式，`prop` 蓝图就得有几张。

        少一张，那一式只剩文字描述、没有图例。这条对应关系一漂，`template_role`
        的槽位声明当场变成假话。
        """
        self.assertEqual(len(sheets.PROP_LAYOUTS), len(sheets.templates_for("prop")))
        body = "".join(sheets.prop_rules())
        for name, spec in sheets.PROP_LAYOUTS:
            self.assertIn(name, body, f"版式「{name}」没进契约正文")
            self.assertIn(spec, body)


class PromptAssemblyTests(unittest.TestCase):
    """分隔符只由 `join_prompt` 补——它是本模块六个提示词构建器与 `refine_asset`
    共用的拼装口。条目自带句尾逗号、作者字段只含空白，都会在千余字的提示词里留下
    断句噪声：不报错、不被任何版式断言看见，只是稀释相邻那条硬约束的权重。"""

    def _all_prompts(self, c, prop, scene):
        return {
            "character": sheets.char_sheet_prompt(c, "写实 3D", n_templates=1),
            "prop": sheets.prop_sheet_prompt(prop, "写实 3D", n_templates=1),
            "scene": sheets.scene_sheet_prompt(scene, "写实 3D"),
            "expression": sheets.expression_sheet_prompt(c, "写实 3D"),
            "pose": sheets.pose_sheet_prompt(c, "写实 3D"),
            # 俯视图与其余各类同收画风前缀（经 prefix_for 补职责声明）
            "topview": sheets.scene_topview_prompt(scene, "写实 3D"),
        }

    def _assert_clean(self, prompts):
        for name, prompt in prompts.items():
            self.assertIsNone(re.search(r"，\s*，", prompt),
                              f"{name} 有空条目、只含空白的条目或多余的句尾逗号")
            self.assertNotIn("；，", prompt, f"{name} 的分隔符叠了两层")
            self.assertFalse(prompt.endswith("，"), f"{name} 以分隔符收尾")

    def test_populated_fields_assemble_cleanly(self):
        self._assert_clean(self._all_prompts(
            {"name": "白刻", "appearance": "银发少年", "outfit": "黑衣",
             "hair": "长发", "role": "剑客", "silhouette_notes": "左肩护甲"},
            {"desc": "一柄长刃", "kind": "weapon"}, "黄昏的钟楼"))

    def test_blank_and_padded_fields_never_leak_empty_segments(self):
        """作者字段来自 CLI 与 Studio 自由文本，空串、纯空格与首尾空白都到得了这里。"""
        self._assert_clean(self._all_prompts(
            {"name": "甲", "appearance": "   ", "outfit": "", "hair": "  长发  ",
             "role": None, "silhouette_notes": " "},
            {"name": "碗", "desc": "  "}, "  钟楼  "))
        p = sheets.char_sheet_prompt({"name": "甲", "appearance": "  银发  "},
                                     "  写实 3D  ")
        self.assertTrue(p.startswith("写实 3D，"), "前缀两端空白要吃掉")
        self.assertIn("，银发，", p, "作者字段两端空白要吃掉")

    def test_refine_shares_the_same_assembly_point(self):
        """局部改造拼的是同一批条目，分隔符纪律不该另起一套。"""
        import inspect

        from kinema import refine
        self.assertIn("sheets.join_prompt(", inspect.getsource(refine.refine_asset))


class CharacterSheetContractTests(unittest.TestCase):
    def test_three_zone_layout_is_pinned_without_drawn_rules(self):
        """三区靠留白分区，不靠画线：一旦获准画栏框，模型就会把栏框升级成带标题栏
        的表格边框——那正是被禁的文字层的入口。"""
        prompt = sheets.char_sheet_prompt(
            {"name": "测试角色", "appearance": "中性测试角色", "outfit": "简洁服装"},
            "2D 手绘", n_templates=1)
        for token, why in (
            ("40% : 30% : 30%", "三区宽度比是版式骨架"),
            ("锁骨以上正面肖像大特写", "肖像取景范围决定面部锚点的分辨率"),
            ("肖像穿着与全身像完全相同的服装",
             "取景裁到锁骨是构图语句不是着装语句——缺这句时"
             "「不出现肩臂」会被执行成裸肩画到画框边"),
            ("绝不画成裸肩、裸上身", "着装要求必须带显式禁形，不能只靠正说"),
            ("中区正面、右区背面", "两视的朝向分配是版式的一部分"),
            ("八头身", "跨视图一致性只约束两像相同，不约束比例本身"),
            ("不画任何竖向分隔线", "分区只由留白表达"),
            ("底部对齐、等高等比例", "两个全身像必须可比"),
            ("留白目视相等", "版面密度口径：不拥挤也不空旷"),
        ):
            self.assertIn(token, prompt, why)

    def test_side_view_grid_and_swatches_are_gone(self):
        """角色版式不设侧视、细节格与色板槽位：官方人物参考口径是
        「大头照 + 全身照即可，不建议人物多视图」，多视图加剧 ID 漂移；武器与
        物件归独立设定图。契约里复活任何一个，蓝图与提示词就自相矛盾。"""
        body = "".join(sheets.char_rules()) + "".join(sheets.char_tail())
        self.assertNotIn("侧面", body, "版式不含侧视")
        self.assertNotIn("细节格", body.replace("细节小格", ""), "版式不设细节格槽位")
        self.assertNotIn("等距规整网格", body)
        self.assertNotIn("配色色板", body, "版式不设色板槽位")
        self.assertNotIn("turnaround", body)
        # 删槽位不等于放开：负面禁令必须显式在（模型照设定集惯例会自己补）
        self.assertIn("不出现色板色块、细节小格", body)
        # 武器条款从「只进细节格」收紧为无条件空手
        self.assertIn("双手空手", body)
        self.assertIn("不持握、不佩戴、不背挂任何武器或道具", body)

    def test_leg_length_is_pinned_by_landmark_lines(self):
        """决定腿长的是胯线高度，不是头的份数：缺比例契约时模型按训练集均值落笔，
        胯线掉到全高 0.56~0.59、腿长只剩四成。写实档的角色设定图走纯文生图、不垫
        版式蓝图，这段文字是那一档唯一的比例通道，故必须逐段给出可数的高度——
        「八头身」是总量声明，模型对它的响应远弱于一串能数的段落。"""
        spec = sheets.figure_proportions()
        for token in ("八头身", "头顶到下巴 1 头", "脐到胯线（裆部）1 头",
                      "胯线到膝 2 头", "膝到脚底 2 头",
                      "可略高绝不可低", "3:4", "腿长占全高一半"):
            self.assertIn(token, spec)
        # 腿短是模型的强先验，只正说压不住：禁形必须显式在
        self.assertIn("绝不把胯线画到全高一半以下", spec)
        self.assertIn("绝不画成躯干长、腿短的体型", spec)
        for rules in (sheets.char_rules(), sheets.pose_rules("")):
            self.assertIn(spec, rules, "比例契约要独立成条，不能并进别的规则稀释权重")
        # 前位 token 权重最高，而写实档没有蓝图兜底——比例契约必须紧跟版式总纲，
        # 不能埋在二十几条版式规则之后
        self.assertLessEqual(sheets.char_rules().index(spec), 1,
                             "比例契约要排在版式总纲之后的第一条")
        # 人台的比例本身是对的（胯线正在正中）。禁令的宾语必须收窄到具体维度——
        # 「一笔都不许从样板取材」再补一句例外，两句相邻打架，模型只执行更强的那条
        role = sheets.template_role("character")
        self.assertIn("可以且只可以对齐它的水平定位线高度", role)
        self.assertIn("这几维一律只按下文的角色描述画", role)
        self.assertIn("脸型、五官、发型、性别、年龄感、体型胖瘦", role)

    def test_required_visual_traits_reach_the_defining_sheet(self):
        """`visual_requirements` 是身份核对的正向清单：分镜侧逐镜要求「必须保留」，
        而定义外观的定稿表若收不到它，下游每一镜都在保留一个设定图上不存在的特征。"""
        p = sheets.char_sheet_prompt(
            {"name": "岑舟", "appearance": "东亚女性",
             "visual_requirements": ["左眉骨旧疤", " ", "左胸褪色圆形徽章"]},
            "写实 3D")
        self.assertIn("左眉骨旧疤；左胸褪色圆形徽章", p)
        self.assertNotIn("；；", p, "空白项不许拼出空段")
        self.assertNotIn("必须画出的视觉特征",
                         sheets.char_sheet_prompt({"name": "甲"}, "写实 3D"),
                         "没登记就不出这一句")

    def test_full_body_views_never_inherit_the_blueprint_lineart(self):
        """全身像是**成品**：上色、着装、配色与肖像一致。灰模 / 线稿是蓝图自身的
        属性，一旦写进契约，两个立像就会被渲成没有材质的素模或未上色草图——
        这正是整套「去身份样板 + 职责声明」要拦的那类泄漏。"""
        body = "".join(sheets.char_rules()) + "".join(sheets.char_tail())
        for leak in ("白模", "灰模", "素模", "线稿"):
            self.assertNotIn(leak, body, f"蓝图属性「{leak}」泄进了成品契约")
        self.assertIn("服装衣纹、配色与体积结构逐视图交代完整", body)
        self.assertIn("发色瞳色与服装（含衣领与领口样式）严格一致", body)
        # 头身比只在两个全身像之间可比——肖像取景到锁骨，没有身可比
        self.assertIn("两个全身视图之间头身比与体型完全一致", body)
        self.assertNotIn("肖像与两个全身视图之间头身比", body)


class PropSheetContractTests(unittest.TestCase):
    def test_single_layout_is_structural_three_view(self):
        """道具版面只有结构三视一式：等大并列才比得出长度与部件的比例关系。
        体积物件失去主视图层次由细节框补交代，是已知代价——换来契约、样板与
        守卫的一一对应。上 2/3 三视、下 1/3 细节框，不设色板槽位。"""
        body = "".join(sheets.prop_rules(True))
        self.assertIn("结构三视式", body)
        self.assertIn("等高等大横向并列", body)
        self.assertIn("三分之二", body)
        self.assertIn("三分之一", body)
        for gone in ("择式", "两式", "转台式", "主次分明"):
            self.assertNotIn(gone, body, f"多式版面的旧措辞「{gone}」不得残留")
        # 不设色板槽位时负面禁令必须显式在：模型照设定集惯例会自发补一条色带
        self.assertNotIn("配色色板", body)
        self.assertIn("不出现色板色块、颜色条带", body)

    def test_brand_marks_are_forbidden(self):
        """品类词会把模型拉向训练集里最强的真实品牌（便利店纸杯直出 7-Eleven
        仿标）：物品自身的标识必须是虚构中性图形，商标是发布风险也撞 IP 审核。"""
        for weapon in (False, True):
            body = "".join(sheets.prop_rules(weapon))
            self.assertIn("不得模仿任何真实品牌的商标", body)
            self.assertIn("标识内不含可读文字或字母", body)

    def test_completeness_and_proportion_are_pinned(self):
        """失败形态是主体斜置**出画**、剑尖被裁在画外——被裁的那张交代不了
        形制；比例一失真（长剑画成短剑），设定图就失去了参照价值。"""
        body = "".join(sheets.prop_rules(True))
        for token, why in (
            ("完整置于所属槽位正中", "完整视图在自己槽位里居中"),
            ("绝不被画面边缘裁切", "失败形态：主体斜置出画"),
            ("定位视图一律摆正", "正/侧/背是量比例用的，不许倾斜"),
            ("把长剑画成短剑", "比例失真的具体反例要写死"),
            ("每个观察角度只画一次", "同一角度不许重复"),
            ("细节框只画局部放大、完整视图只画整体全貌", "两类槽位的职责分界"),
        ):
            self.assertIn(token, body, why)
        self.assertNotIn("角度小图", body, "版式改写后这个称呼已无定义，不得复活")


class ImageOnlyContractTests(unittest.TestCase):
    """设定图是纯图片输出：生图模型手写文字的两大事故是错字与编造，而设定图上的
    文字没有下游消费者——名称、条目、色值全在 project.json 里，画一遍只会引入
    一处永远对不上的信息。"""

    def test_character_and_prop_sheets_carry_no_text_layer(self):
        for body in ("".join(sheets.char_rules()) + "".join(sheets.char_tail()),
                     "".join(sheets.prop_rules(True))):
            self.assertIn("完整的纯图片设定图", body)
            self.assertIn("禁止任何文字、标题、简介、信息栏、编号、数字、"
                          "logo、水印或签名", body)

    def test_object_name_identifies_but_never_becomes_pixels(self):
        """无描述时名称是唯一的对象识别词，故仍要进提示词；但它不再是画面文字的
        数据源，别以「名称：」这类数据行形态出现——那是标注层的接口形状。"""
        p = sheets.char_sheet_prompt({"name": "白刻"}, "画风")
        self.assertIn("白刻", p)
        self.assertNotIn("角色名称：", p)
        pr = sheets.prop_sheet_prompt({"name": "单分子刃"}, "画风")
        self.assertIn("单分子刃", pr)
        self.assertNotIn("道具名称：", pr)

    def test_scene_and_extra_sheets_stay_text_free_too(self):
        self.assertIn("无文字标注", "".join(sheets.scene_rules()))
        for kind in ("expression", "pose", "topview"):
            self.assertIn("无文字标注", "".join(sheets.rules_for(kind)))


if __name__ == "__main__":
    unittest.main()
