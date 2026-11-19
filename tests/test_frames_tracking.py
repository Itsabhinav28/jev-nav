import cv2
import numpy as np

from jev_nav.frames import JEV_MAX_SIDE, JEV_MIN_SIDE, assess_quality, prepare_for_jev
from jev_nav.schemas import BBox, Detection, PerceptionFindings
from jev_nav.tracking import Tracker


def _decode(jpeg: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def test_prepare_fits_jev_limits():
    for shape in [(2160, 3840), (100, 160), (1080, 1920), (5000, 400)]:
        img = _decode(prepare_for_jev(np.random.randint(0, 255, (*shape, 3), np.uint8)))
        h, w = img.shape[:2]
        assert max(h, w) <= JEV_MAX_SIDE and min(h, w) >= JEV_MIN_SIDE, (shape, img.shape)


def test_quality_gate():
    assert assess_quality(np.zeros((480, 640, 3), np.uint8)).reason == "too_dark"
    assert assess_quality(np.full((480, 640, 3), 128, np.uint8)).reason == "blurry"
    assert assess_quality(np.random.randint(0, 255, (480, 640, 3), np.uint8)).ok


def _perc(box: BBox | None) -> PerceptionFindings:
    none = lambda k: Detection(label=k, presence=0.0)  # noqa: E731
    return PerceptionFindings(
        person_ahead=Detection(label="person_ahead", presence=0.9 if box else 0.0, box=box),
        stairs_up=none("stairs_up"), stairs_down=none("stairs_down"), path_obstacle=none("path_obstacle"),
        ground_hazard=none("ground_hazard"), overhead_hazard=none("overhead_hazard"), crossing_object=none("crossing_object"),
    )


def test_ttc_from_box_growth():
    tr = Tracker()
    # Box area grows 0.04 → 0.09 in 1 s: TTC = 2·0.09 / 0.05 = 3.6 s
    tr.update(0.0, _perc(BBox(top=0.4, bottom=0.6, left=0.4, right=0.6)))
    info = tr.update(1.0, _perc(BBox(top=0.35, bottom=0.65, left=0.35, right=0.65)))
    assert abs(info["person_ahead"].ttc_s - 3.6) < 0.01


def test_receding_object_has_no_ttc_and_is_remembered_when_it_leaves():
    tr = Tracker()
    tr.update(0.0, _perc(BBox(top=0.3, bottom=0.9, left=0.0, right=0.3)))
    info = tr.update(0.5, _perc(BBox(top=0.35, bottom=0.88, left=0.0, right=0.25)))
    assert info["person_ahead"].ttc_s is None
    gone = tr.update(1.0, _perc(None))
    assert gone["person_ahead"].remembered_side == "left"


def test_box_jumping_to_another_object_is_not_an_approach():
    tr = Tracker()
    tr.update(0.0, _perc(BBox(top=0.6, bottom=0.7, left=0.4, right=0.5)))
    info = tr.update(0.5, _perc(BBox(top=0.1, bottom=0.7, left=0.1, right=0.5)))  # area ×16 in one frame
    assert info["person_ahead"].ttc_s is None


def test_far_boxes_have_no_ttc():
    tr = Tracker()
    tr.update(0.0, _perc(BBox(top=0.45, bottom=0.50, left=0.45, right=0.50)))
    info = tr.update(0.5, _perc(BBox(top=0.44, bottom=0.52, left=0.44, right=0.52)))
    assert info["person_ahead"].ttc_s is None


def test_camera_tilt_is_not_called_climbing():
    from jev_nav.frames import EgoMotionEstimator
    from jev_nav.schemas import UserActivity

    base = np.random.default_rng(3).integers(0, 255, (600, 640, 3), dtype=np.uint8)
    base = cv2.GaussianBlur(base, (7, 7), 0)
    ego = EgoMotionEstimator()
    ego.update(base[0:480])
    assert ego.update(base[12:492]).activity != UserActivity.CLIMBING  # whole scene shifts vertically
