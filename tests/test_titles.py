from cuttlefish.titles import title_from_filename


def test_strips_youtube_id():
    assert (
        title_from_filename("Big Buck Bunny-YE7VzlLtp-4.mp4") == "Big Buck Bunny"
    )
    assert (
        title_from_filename(
            "Sintel - Open Movie by Blender Foundation-eRsGyueVLvQ.webm"
        )
        == "Sintel - Open Movie by Blender Foundation"
    )


def test_strips_release_noise():
    assert (
        title_from_filename("Big.Buck.Bunny.2008.1080p.BluRay.x264-GROUP.mkv")
        == "Big Buck Bunny 2008"
    )
    assert (
        title_from_filename("Some.Show.S01E02.720p.WEB-DL.AAC.x264-RLSGRP.mkv")
        == "Some Show S01E02"
    )


def test_no_extension():
    assert title_from_filename("Big Buck Bunny") == "Big Buck Bunny"


def test_extension_only_does_not_crash():
    # Degenerate input — should produce some string and not raise.
    result = title_from_filename(".mp4")
    assert isinstance(result, str)


def test_keeps_uppercase_8k_token():
    # 8K is not in the noise list (it's a resolution we'd want to know about
    # so the encoder downscales). Keep as-is in the title guess.
    assert (
        title_from_filename("The Daily Dweebs - 8K UHD Stereoscopic 3D-apiu3pTIwuY.mkv")
        == "The Daily Dweebs - 8K UHD Stereoscopic 3D"
    )
