"""HIF 入口恢复：日期变更后的重新登录续跑与主入口超时后的安全场景确认。"""
import time

from utils import logger
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer

from extensions.hif.recovery import state as _hif_recovery

from . import common
from .shop import ProduceHIF__ProduceHIFConsultAuto
from .common import _hif_date_interrupt
from .schedule import ProduceHIF__ProduceHIFTrainAuto, ProduceHIF__ProduceHIFLessonAuto, ProduceHIF__ProduceHIFOptionAuto


@AgentServer.custom_action('ProduceHIF__DateRecoverAuto')
class ProduceHIF__DateRecoverAuto(CustomAction):
    def run(self, context, argv):
        state = _hif_recovery.bind(argv)
        state.date_requested = False
        deadline = time.monotonic() + 180
        phase, stable, previous, date_clicked = 'login', 0, None, False
        # These guards describe pre-login animations, not progress in the saved cultivation.
        ProduceHIF__ProduceHIFTrainAuto._clear_recognition_gate()
        for cls in (ProduceHIF__ProduceHIFLessonAuto, ProduceHIF__ProduceHIFOptionAuto):
            cls._recognition_gate_until = 0.0
            cls._recognition_screen_snapshot = None
        ProduceHIF__ProduceHIFConsultAuto._shop_finished_until = 0.0
        logger.warning('HIF日期变更: 返回标题并登录；从主界面正常继续育成，不跳过准备入口')

        def hit(node, image):
            result = context.run_recognition(node, image)
            return result if result and result.hit else None

        def act(node, result):
            if common._hif_stopping(context) or time.monotonic() >= deadline:
                return False
            action = context.run_action(node, result.box)
            if not action or not action.success:
                logger.error(f'HIF日期恢复: 动作未成功 {node}: {action}')
            return bool(action and action.success)

        try:
            while not common._hif_stopping(context) and time.monotonic() < deadline:
                image = context.tasker.controller.post_screencap().wait().get()
                if time.monotonic() >= deadline:
                    break
                retry = hit('ProduceHIF__ProduceNetworkRetry', image) if common._hif_network_dialog_possible(image) else None
                if retry:
                    if not act('ProduceHIF__ProduceNetworkRetry', retry):
                        break
                elif phase == 'login':
                    date, result = common._hif_date_screen(context, image)
                    home = hit('ProduceHIF__ProduceHomeFlag', image) if not date else None
                    if home:
                        # Bypass ProduceLoop (max_hit) and SkipPreparation without overriding saved options.
                        result = hit('ProduceHIF__ProduceContinue', image)
                        if not result:
                            return common._hif_failure(context, argv.node_name, '日期恢复', '主界面没有继续育成入口，不新开育成')
                        if not act('ProduceHIF__ProduceContinue', result):
                            break
                        phase = 'continue'
                    elif date == 'ProduceHIF__DateChanged':
                        if not date_clicked:
                            if not act(date, result):
                                break
                            date_clicked = True
                    else:
                        # HIF tasks load only their isolated resources, never the upstream startup bundle.
                        startup = context.get_node_data('ProduceHIF__StartUpTestScreen')
                        if not startup:
                            return common._hif_failure(context, argv.node_name, '日期恢复',
                                                'HIF登录恢复节点 ProduceHIF__StartUpTestScreen 不存在')
                        for candidate in startup.get('next', []):
                            node = candidate['name'] if isinstance(candidate, dict) else candidate.split(']')[-1]
                            if node == 'ProduceHIF__ClickCenter' and date != 'ProduceHIF__DateTitle':
                                continue
                            result = hit(node, image)
                            if result:
                                if not act(node, result):
                                    return common._hif_failure(context, argv.node_name, '日期恢复', f'登录动作失败: {node}')
                                if node == 'ProduceHIF__StartUpGetFriendPoints':
                                    phase = 'friend_close'
                                break
                elif phase == 'friend_close':
                    result = hit('ProduceHIF__StartUpCloseButton', image)
                    if result:
                        if not act('ProduceHIF__StartUpCloseButton', result):
                            break
                        phase = 'login'
                elif phase == 'continue':
                    result = hit('ProduceHIF__ProduceChooseContinue', image)
                    if result:
                        if not act('ProduceHIF__ProduceChooseContinue', result):
                            break
                        phase = 'verify'
                    elif common._hif_safe_scene(context, image, exclude=('ProduceHIFMainScreenFlag',)):
                        phase = 'verify'
                else:
                    scene = common._hif_safe_scene(context, image, exclude=('ProduceHIFMainScreenFlag',))
                    stable = stable + 1 if scene and scene == previous else (1 if scene else 0)
                    previous = scene
                    if stable >= 3:
                        if time.monotonic() >= deadline or common._hif_stopping(context):
                            break
                        state.reset(state.task_id)
                        logger.info(f'HIF日期恢复成功: 已确认育成画面 {scene}，重新进入HIF识别')
                        return True
                time.sleep(0.5)
        except Exception as error:
            return common._hif_failure(context, argv.node_name, '日期恢复', f'恢复异常: {error!r}')
        if common._hif_stopping(context):
            return False
        return common._hif_failure(context, argv.node_name, '日期恢复', '180秒内未能完成登录及续育')


@AgentServer.custom_action('ProduceHIF__ProduceHIFEntryObserveAuto')
@_hif_date_interrupt
class ProduceHIF__ProduceHIFEntryObserveAuto(CustomAction):
    def run(self, context, argv):
        _hif_recovery.bind(argv)
        if common._hif_stopping(context):
            return True
        if _hif_recovery.pending:
            return common._hif_failure(context, argv.node_name, _hif_recovery.pending, '提交结果未确认，禁止入口重放')
        if _hif_recovery.deadline is not None and _hif_recovery.now() >= _hif_recovery.deadline:
            return True  # The expiration candidate terminates without another scene recovery.
        if _hif_recovery.deadline is not None or _hif_recovery.entry_recovered:
            frame = common._hif_nonbattle_screencap(context)
            scene = common._hif_safe_scene(context, frame)
            if scene and scene != _hif_recovery.entry_scene and common._hif_observe_result(
                    context, lambda image: common._hif_safe_scene(context, image) == scene):
                _hif_recovery.progress(('已确认场景', scene))
                _hif_recovery.entry_scene = scene
        return True


@AgentServer.custom_action('ProduceHIF__ProduceHIFRecoveryExpiredAction')
@_hif_date_interrupt
class ProduceHIF__ProduceHIFRecoveryExpiredAction(CustomAction):
    def run(self, context, argv):
        return common._hif_failure(context, argv.node_name, '点击前重识别', '30秒恢复预算耗尽')


@AgentServer.custom_action('ProduceHIF__ProduceHIFEntryRecoveryAuto')
@_hif_date_interrupt
class ProduceHIF__ProduceHIFEntryRecoveryAuto(CustomAction):
    def run(self, context, argv):
        state = _hif_recovery.bind(argv)
        if common._hif_stopping(context):
            return True
        if state.pending or state.entry_recovered:
            return common._hif_failure(context, argv.node_name, state.pending or str(state.step), '禁止重放提交/重复超时恢复')
        state.entry_recovered = True
        deadline, previous, stable = state.now() + 30, None, 0
        while not common._hif_stopping(context) and state.now() < deadline:
            if state.deadline is not None and state.now() >= state.deadline:
                break
            image = common._hif_nonbattle_screencap(context)
            scene = common._hif_safe_scene(context, image, deadline=deadline)
            stable = stable + 1 if scene and scene == previous else (1 if scene else 0)
            previous = scene
            if stable >= 3:
                state.entry_scene = scene
                logger.warning(f'HIF重新识别: 主入口超时后已稳定确认 {scene}，返回入口；不执行恢复点击')
                return True
            time.sleep(0.5)
        if common._hif_stopping(context):
            return True
        return common._hif_failure(context, argv.node_name, str(state.step), '主入口30秒恢复未确认安全页面')
