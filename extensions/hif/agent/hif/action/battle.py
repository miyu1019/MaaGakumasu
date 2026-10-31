"""HIF 本战出牌动作：组合识别、用饮、决策三个 mixin 并运行主循环。"""
import time

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer

from extensions.hif.recovery import state as _hif_recovery

from . import common
from .common import HifConfigError, HifDrinkFlowError, _hif_date_interrupt
from .battle_drink import BattleDrinkMixin
from .battle_decision import BattleDecisionMixin
from .battle_recognition import BattleRecognitionMixin


@AgentServer.custom_action("ProduceHIF__ProduceCardsAuto")
@_hif_date_interrupt
class ProduceHIF__ProduceCardsAuto(BattleRecognitionMixin, BattleDrinkMixin, BattleDecisionMixin, CustomAction):
    """
    HIF 本战出牌动作。

    识别与操作、本战用饮、出牌决策分别在 BattleRecognitionMixin、BattleDrinkMixin、
    BattleDecisionMixin 中；本类负责状态初始化与主循环 _run_battle。
    """

    def __init__(self):
        super().__init__()
        self._last_turn_count = None     # 上一次可信的剩余回合数，用于排除 OCR 异常跳变
        self._pending_extra_turns = None
        self._extra_turn_read_confirmed = False
        self._extra_turn_transition = False
        self._extra_rollover_seen = None
        self._extra_rollover_reserve = None
        self._full_power_hold_source = None
        self._full_power_hand_hold_pending = False
        self._full_power_hand_hold_retries = 0
        self._active_pointer_state = None
        self._full_power_value = None
        self._pending_card_hand = None
        self._pending_card_hand_count = 0
        self._pending_card_hand_started_at = None
        self.priority_index = {}      # key -> 顺序（越小越优先）
        self.unknown_priority = 11    # HIF 当前职业未配置/暂未识别卡的兜底优先级，沿用原配置字段
        self.unconfigured_card_preference = {}
        self._hif_card_types = {}
        self._hif_last_upgraded = None
        self.conditional_priority_rules = {}  # 当前职业卡牌的条件优先级
        self.no_extra_turn_keys = set()
        self.use_conditions = {}  # 当前职业：组内条件同时满足，组间满足任一组即可出牌
        self.fallback = "suggestion"  # 兜底：suggestion / best_score
        self.profession = self.DEFAULT_PROFESSION
        self.drink_turn = self.DRINK_TURN
        self._drink_catalog_by_key = {}
        self._drink_imported_names = []
        self._drink_specific_timings = {}
        self._drink_disabled_names = set()
        self._drink_default_timing = {"mode": "never"}
        self._drink_first_battle_policy = {"enabled": True, "max_uses": -1, "unconfigured_max_uses": -1}
        self._drink_first_battle_limits = {}
        self._drink_first_battle_enabled = {}
        self._drink_second_battle_disabled = set()
        self._drink_used_counts = {}
        self._drink_attempted_triggers = set()
        self._drink_before_targets = []
        self._followups = None
        self._followup_skip_turn = None
        self._followup_skip_deadline = None
        self._followup_move_targets = None
        self._paired_card = None
        self._paired_unusable_count = 0
        self._paired_deadline = None
        self.hif_battle_skip = False  # 当前场是否跳过，由 SKIP本战1/2 与初始回合数决定
        self.hif_battle_number = None
        self._hif_debug_enabled = False
        self._hif_name_index = {}
        self._reset_hif_recognition()
        # Agent 启动注册时即实例化；此处配置无效只记日志，开战时 _run_battle 会重新严格加载。
        try:
            self._load_config()
        except HifDrinkFlowError as exc:
            logger.warning(f"HIF出牌策略预加载失败，开战时将重新校验: {exc}")

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        _hif_recovery.bind(argv)
        try:
            return self._run_battle(context, argv)
        except HifDrinkFlowError as exc:
            if context is not None and getattr(context.tasker, 'stopping', False):
                logger.info('HIF本战饮料: 任务已停止，取消后续操作')
                return False
            node, stage = getattr(argv, 'node_name', '?'), _hif_recovery.pending or '本战'
            if isinstance(exc, HifConfigError):
                return common._hif_failure(context, node, stage, str(exc))
            # 用饮待确认时，饮料详情仍开着就无法判断是否已使用；详情关闭并回到已知场景再交回入口。
            still_there = (lambda image: self._is_drink_detail_open(context, image)) if _hif_recovery.pending else None
            return common._hif_timeout(context, node, stage, str(exc), still_there=still_there)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.exception(f'HIF本战配置/状态异常: {exc!r}')
            return common._hif_failure(context, getattr(argv, 'node_name', '?'), _hif_recovery.pending or '基础策略/本战', str(exc))
        finally:
            self._followups = None
            self._paired_card = None
            self._paired_deadline = None
            self._followup_skip_turn = None
            self._followup_skip_deadline = None
            self._followup_move_targets = None
            self._pending_extra_turns = None
            self._extra_turn_read_confirmed = False
            self._extra_turn_transition = False
            self._extra_rollover_seen = None
            self._extra_rollover_reserve = None

    def _run_battle(self, context: Context, argv: CustomAction.RunArg) -> bool:
        self._hif_debug_enabled = self._load_hif_debug(context)
        self._reset_hif_recognition()
        self._load_config(self._load_hif_profession(context))
        self._load_hif_battle_drink_policy()
        self._load_hif_followups()
        if self._followups is not None:
            logger.info(f"HIF组合配置: {len(self._followups.rules)}组，等待回合按各组设置")
        # 开场演出结束后，要求 SKIP 按钮连续稳定出现，才读取初始回合数并开始 SKIP/出牌。
        # 这样不会把开场动画帧误判为可操作画面。
        self._wait_until_playable(context, confirmation_count=self.SKIP_STABLE_COUNT)
        time.sleep(0.4)

        # 重置本场状态（避免跨战斗泄漏）
        self._drink_sembri_done = False  # 跨战斗清理: 本场是否试过喝センブリソーダ(锁定一次,防重复)
        self._drink_buff_done = False    # 跨战斗清理: 本场喝buff是否已执行(锁定只喝一次,防倒数2/1回合重复判断)
        self._drink_attempted_triggers = set()
        self._followup_skip_turn = None
        self._drink_used_counts = {}
        self._followup_skip_deadline = None
        self._followup_move_targets = None
        self._paired_card = None
        self._paired_deadline = None
        self._paired_drink_disabled = False
        self._last_turn_count = None     # 跨战斗清理: 不沿用上一场的回合数
        self._pending_extra_turns = None
        self._extra_turn_read_confirmed = False
        self._extra_turn_transition = False
        self._extra_rollover_seen = None
        self._extra_rollover_reserve = None
        self._battle_transition_misses = 0
        self._battle_transition_taps = 0
        self._last_battle_transition_tap = 0.0
        self._active_pointer_state = None
        self._full_power_value = None
        self._full_power_hold_source = None
        self._full_power_hand_hold_pending = False
        self._full_power_hand_hold_retries = 0
        self._reset_card_hand_stability()
        start_image = common._hif_nonbattle_screencap(context)
        self.hif_battle_number = self._detect_battle_number(context, start_image)
        self.hif_battle_skip = self._load_hif_battle_skip(context, self.hif_battle_number)
        if self.hif_battle_number is None:
            logger.warning("HIF战斗: 未能以初始回合数识别本战1/2，安全起见不启用SKIP")
        elif self.hif_battle_skip:
            logger.info(
                f"HIF SKIP本战{self.hif_battle_number}已开启："
                "本场不出牌，所有可操作回合均点击SKIP"
            )
        else:
            logger.info(f"HIF战斗: 初始回合数判定为本战{self.hif_battle_number}，正常出牌")
        if not self.hif_battle_skip and self._last_turn_count in self.BATTLE_START_TURNS:
            self._process_battle_drink_timing(context, self._last_turn_count, first_turn=True)
        elif not self.hif_battle_skip and self._last_turn_count is not None:
            logger.info("HIF本战饮料: 首次可操作时已非本场第1回合，不补用开场时机饮料")
        _battle_end_count = [0]  # 「战斗结束」次へ模板 连续命中计数（跨帧累计）
        self._battle_progress_at = _hif_recovery.now()
        self._battle_recovery_deadline = None
        self._battle_progress_signature = None
        self._battle_confirmed_plays = 0
        _hif_recovery.progress(('本战', self.hif_battle_number))
        while True:
            # 处理手动终止任务
            if context.tasker.stopping:
                logger.info("任务中断")
                return True
            if self._paired_card is not None and _hif_recovery.now() >= self._paired_deadline:
                self._recover_paired_drink(context)

            # 截图
            image = common._hif_nonbattle_screencap(context)

            # 反向判定战斗是否结束：检测到结算界面（排名榜/次へ按钮）才退出出牌。
            # 不再依赖数字环/体力条/绿心/SKIP 等"战斗中信号"（它们都会变或消失），
            # 而"次へ橘色按钮"只在战斗结束的结算界面出现（战斗中恒为 miss），方向反过来最稳定。
            if self._is_battle_end(context, image, consecutive=_battle_end_count):
                if self._paired_card is not None:
                    self._recover_paired_drink(context)
                logger.info("检测到结算界面（次へ）")
                logger.success("事件: 退出本战处理")
                _hif_recovery.progress(('结算', self.hif_battle_number))
                break
            if self._check_battle_progress(context, image):
                continue
            if _battle_end_count[0]:
                time.sleep(0.2)
                continue

            # HIF 本战SKIP：不识别或打出任何技能卡，只在可操作时点击游戏内 SKIP。
            # 仍保留结算页的处理。
            if self.hif_battle_skip:
                skip_reco = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
                if skip_reco and skip_reco.hit:
                    self._battle_transition_misses = 0
                    self._read_turn_count(context, image)
                    logger.info(f"HIF SKIP本战{self.hif_battle_number}：跳过当前回合")
                    self._hif_skip_reason = "配置跳过本战"
                    self._skip_round(context)
                else:
                    self._skip_battle_end_animation(context, image)
                    time.sleep(0.3)
                continue

            # 移动/保留技能卡界面：必须在战斗识别前处理，否则会被当成"无手牌/卡死"误判。
            if self._handle_move_cards(context, image):
                self._reset_hif_recognition()
                logger.info("移动界面处理完成,继续")
                continue

            if self._is_drink_detail_open(context, image):
                if not self._close_drink_detail(context):
                    self._abort_drink_flow(context, "饮料详情无法关闭，停止当前HIF任务")
                self._reset_hif_recognition()
                continue

            # 「能看见 SKIP 才校验手牌」：SKIP = 可出牌/可跳过的标志。SKIP 不可见说明
            # 处于动画过渡/非可出牌状态，此时识别手牌会漏框（YOLO 框不到 cards），
            # 导致误判"无手牌"而 skip（即"出完牌后下一回合直接跳过"）。故 SKIP 不可见时不进识别，等下一帧。
            skip_reco = context.run_recognition("ProduceHIF__ProduceRecognitionSkipRound", image)
            if not (skip_reco and skip_reco.hit):
                self._reset_card_hand_stability()
                # 单帧 SKIP 漏检不证明换手；实际动作/换回合才清空补认进度。
                self._hif_empty_started_at = None
                self._hif_empty_count = 0
                self._hif_empty_timeout_reported = False
                self._hif_skip_reason = None
                self._hif_unusable_signature = None
                self._hif_unusable_count = 0
                self._skip_battle_end_animation(context, image)
                time.sleep(0.3)
                continue
            self._battle_transition_misses = 0
            # 过场已没有可操作 HUD，不能把背景文字当成剩余回合数。
            turn = self._read_turn_count(context, image)
            if turn is not None and turn != self._hif_recognition_turn:
                self._reset_hif_recognition()
                self._hif_recognition_turn = turn
            self._observe_followup_turn(turn)
            if self._paired_card is None and self._process_battle_drink_timing(context, turn):
                self._reset_hif_recognition()
                continue

            # 识别手牌
            reco_detail = context.run_recognition("ProduceHIF__ProduceRecognitionCards", image)
            results = list(reco_detail.all_results or []) if reco_detail else []
            kept = self._hif_card_boxes(results)
            self._release_unusable_paired_card(results)
            if not kept:
                if self._hif_no_playable_hand(context, image, results):
                    no_playable_reason = self._hif_skip_reason
                    if self._paired_card is not None:
                        time.sleep(self.CARD_HAND_POLL_INTERVAL)
                        continue
                    if self._followups is not None and self._followups.active:
                        if self._decide(context, [])['type'] == 'retry':
                            time.sleep(self.CARD_HAND_POLL_INTERVAL)
                            continue
                    self._hif_skip_reason = no_playable_reason
                    self._skip_round(context)
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
                continue
            self._hif_unusable_signature = None
            self._hif_unusable_count = 0

            # 获取卡牌信息（建议牌/最高分牌，用于兜底）
            suggestions, useless, cards, suggestions_box, best_box = self._get_card_info(results)

            if self._hif_hand_geometry and not self._hif_same_geometry(kept):
                self._hif_log_detection(image, results, "手牌数量或横向位置变化")
            self._hif_bind_hand(kept)
            self._hif_empty_started_at = None
            self._hif_empty_count = 0
            self._hif_empty_timeout_reported = False
            self._hif_skip_reason = None

            # 识别每张可出牌（cards）的身份
            identified = []
            for index, (box, conf) in enumerate(kept):
                self._hif_last_upgraded = None
                if index in self._hif_detail_keys:
                    key, score = self._hif_detail_keys[index], 0.0
                    upgraded = self._hif_detail_upgraded.get(index)
                else:
                    key, score = self._identify_card(context, image, box)
                    upgraded = self._hif_last_upgraded
                identified.append({"box": box, "key": key, "score": score, "conf": conf, "upgraded": upgraded})

            stability = self._confirm_card_hand(identified)
            if stability == "wait":
                time.sleep(self.CARD_HAND_POLL_INTERVAL)
                continue
            paired_visible = self._paired_card is not None and any(card['key'] == self._paired_card['key'] for card in identified)
            if not paired_visible and self._hif_probe_unknowns(
                context, image, identified, detection_results=results,
            ):
                self._reset_card_hand_stability()
                continue

            self._active_pointer_state = (
                self._read_active_pointer_state(context, image)
                if self.profession in ("全力", "強気")
                else None
            )
            if self.profession == "全力":
                self._full_power_value = self._read_full_power_value(context, image)
            decision = self._decide(context, identified)
            if decision['type'] == 'play' and self._prepare_paired_drink(context, decision):
                self._reset_hif_recognition()
                continue
            before_play = {name: getattr(self, name) for name in (
                "_full_power_hold_source", "_full_power_hand_hold_pending", "_full_power_hand_hold_retries")}
            if decision["type"] == "play":
                box = decision["box"]
                if self.profession == "全力":
                    self._full_power_hold_source = (
                        decision.get("key") or self.FULL_POWER_UNKNOWN_HOLD_SOURCE
                    )
                    if self._full_power_hold_source not in (
                        self.FULL_POWER_DECK_HOLD_SOURCE, "本領発揮+"
                    ):
                        self._full_power_hand_hold_pending = True
                        self._full_power_hand_hold_retries = 0
                    else:
                        self._full_power_hand_hold_pending = False
                        self._full_power_hand_hold_retries = 0
                time.sleep(1)  # 防止点击过早导致只命中一次
                played = self._play_a_card(context, box)
                if not played:
                    for name, value in before_play.items():
                        setattr(self, name, value)
                    continue
                if self._followups is not None:
                    self._followup_skip_turn = None
                    self._followup_skip_deadline = None
                    self._followups.committed(decision.get('key'), decision.get('followup_role'))
                    self._paired_card = None
                    self._paired_deadline = None
            elif decision["type"] == "fallback":
                self._play_fallback(context, image, results)
            elif decision["type"] == "skip":
                if useless:
                    self._hif_skip_reason = (self._hif_skip_reason or "策略等待") + "；另有游戏内不可用牌"
                self._skip_round(context)
            elif decision["type"] == "retry":
                # 组合等待或配对出牌确认中；不出牌也不空过，短暂等待后重截。
                self._reset_card_hand_stability()
                time.sleep(0.2)
                continue
            time.sleep(self.CLICK_DELAY)

        return True

    def _check_battle_progress(self, context, image) -> bool:
        """Clicks/animations are not progress; confirmed turns, plays and uses are."""
        if common._hif_retry_network(context, image):
            return True  # Discard the stale network-dialog image after retry.
        turn = self._last_turn_count
        signature = (turn, self._battle_confirmed_plays, sum(self._drink_used_counts.values()))
        now = _hif_recovery.now()
        old = self._battle_progress_signature
        if old is None or (turn is not None and turn != old[0]) or signature[1:] != old[1:]:
            self._battle_progress_signature = (turn if turn is not None else (old[0] if old else None), *signature[1:])
            self._battle_progress_at = now
            self._battle_recovery_deadline = None
        if now - self._battle_progress_at < 120:
            return False
        if self._battle_recovery_deadline is None:
            logger.warning('HIF本战: 120秒无确认业务进展，进入30秒状态恢复；不重放消费')
            self._battle_recovery_deadline = now + 30
        if now >= self._battle_recovery_deadline:
            self._abort_drink_flow(context, 'HIF本战: 30秒状态恢复仍无业务进展，保留现场停止')
        # The main loop handles known details, move/hold and result pages during this window.
        return False
