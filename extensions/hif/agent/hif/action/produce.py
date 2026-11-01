"""HIF 自定义动作汇总：导入各模块完成注册，并保持原有的导出名称。

各模块自行导入所依赖的模块，此处的导入顺序不影响注册结果。
"""
from .deck import (  # noqa: F401
    ProduceHIF__ProduceHIFCardSwapAuto,
    ProduceHIF__ProduceHIFExchangeAuto,
    ProduceHIF__ProduceHIFCardDeleteAuto,
    ProduceHIF__ProduceHIFCardCopyAuto,
    ProduceHIF__ProduceHIFSupplyCardAuto,
)
from .shop import (  # noqa: F401
    ProduceHIF__ProduceHIFConsultAuto,
    ProduceHIF__ProduceHIFCardCustomAuto,
    ProduceHIF__ProduceHIFConsultShopAuto,
)
from .battle import (  # noqa: F401
    ProduceHIF__ProduceCardsAuto,
)
from .common import (  # noqa: F401
    EXT_DIR,
    BASE_DIR,
    _HIF_SAFE_SCENES,
    HIF_DRINK_CATALOG_PATH,
    HIF_DRINK_PROFILES_PATH,
    HIF_PRIORITY_CATALOG_PATH,
    CARDS_PRIORITY_CONFIG_PATH,
    HifDrinkFlowError,
    _hif_defer,
    _hif_failure,
    _hif_recovery,
    _hif_stopping,
    _hif_safe_scene,
    _HifDateChanged,
    _hif_date_screen,
    _hif_retry_network,
    _hif_date_interrupt,
    _hif_drink_name_key,
    _hif_observe_result,
    _hif_title_possible,
    _hif_nonbattle_screencap,
    _hif_network_dialog_possible,
)
from .drinks import (  # noqa: F401
    ProduceHIF__ProduceHIFDrinkAuto,
    ProduceHIF__ProduceHIFKeepDrinkAuto,
    _hif_drink_priority_names,
)
from .schedule import (  # noqa: F401
    ProduceHIF__ProduceHIFRestAuto,
    ProduceHIF__ProduceHIFTrainAuto,
    ProduceHIF__ProduceSkipShowAuto,
    ProduceHIF__ProduceHIFLessonAuto,
    ProduceHIF__ProduceHIFOptionAuto,
    ProduceHIF__ProduceUsePtClickAuto,
    ProduceHIF__ProduceChooseEventBase,
    ProduceHIF__ProduceUseItemClickAuto,
    ProduceHIF__ProduceUseNoteClickAuto,
    ProduceHIF__ProduceChooseOptionsAuto,
    ProduceHIF__ProduceHIFHomeActionBase,
    ProduceHIF__ProduceHIFReChallengeAuto,
    ProduceHIF__ProduceHIFClickRecoBoxAuto,
)
from .entry_recovery import (  # noqa: F401
    ProduceHIF__DateRecoverAuto,
    ProduceHIF__ProduceHIFEntryObserveAuto,
    ProduceHIF__ProduceHIFEntryRecoveryAuto,
    ProduceHIF__ProduceHIFRecoveryExpiredAction,
)
