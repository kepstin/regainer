#!/usr/bin/env python3

# regainer - advanced ReplayGain tagging
# Copyright 2016 Calvin Walton <calvin.walton@kepstin.ca>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Advanced ReplayGain scanner and tagger"""

__version__ = "1.0.0"

import subprocess
import argparse
import asyncio
import multiprocessing
import re
from collections import deque
import mutagen
from math import log10
from enum import Enum
import sys
import logging
import decimal

logger = logging.getLogger(__name__)


class AlbumAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super(AlbumAction, self).__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        if namespace.album is None:
            namespace.album = deque()
        if option_string == "-a" or option_string == "--album":
            namespace.album.append({"track": deque(values), "exclude": deque()})
        if option_string == "-e" or option_string == "--exclude":
            if len(namespace.album) == 0:
                if namespace.exclude is None:
                    namespace.exclude = values
                else:
                    namespace.exclude.extend(values)
            else:
                namespace.album[-1]["exclude"].extend(values)


class TrackAction(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super(TrackAction, self).__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        if namespace.track is None:
            namespace.track = deque(values)
        else:
            namespace.track.extend(values)


class GainInfo:
    def __init__(self, loudness=None, album_loudness=None, peak=None, album_peak=None):
        self.loudness = loudness
        self.album_loudness = album_loudness
        self.peak = peak
        self.album_peak = album_peak

    def __repr__(self):
        return "GainInfo(loudness={}, peak={}, album_loudness={}, album_peak={})".format(
            repr(self.loudness),
            repr(self.peak),
            repr(self.album_loudness),
            repr(self.album_peak),
        )

    def __str__(self):
        str = ""
        str += "Track: "
        if self.loudness is None:
            str += "I: None"
        else:
            str += "I: {:.2f} LUFS".format(self.loudness)
        str += ", "
        if self.peak is None:
            str += "Peak: None"
        else:
            str += "Peak: {:.2f} dBFS".format(self.peak)
        str += "; Album: "
        if self.album_loudness is None:
            str += "I: None"
        else:
            str += "I: {:.2f} LUFS".format(self.album_loudness)
        str += ", "
        if self.album_peak is None:
            str += "Peak: None"
        else:
            str += "Peak: {:.2f} dBFS".format(self.album_peak)
        return str


class OggOpusMode(Enum):
    R128 = 1
    """
    Write R128 gain tags as specified in the Ogg Opus encapsulation doc.

    This writes the tags R128_TRACK_GAIN and R128_ALBUM_GAIN to the file,
    and removes any REPLAYGAIN tags that may be present. The R128 gain tags
    use the EBU R128 -23 LUFS reference level.

    This method is the most standards compliant, but has limited application
    compatibility.
    """

    REPLAYGAIN = 2
    """
    Write REPLAYGAIN tags compatible with FLAC, Vorbis, etc.

    This writes the tags REPLAYGAIN_{TRACK,ALBUM}_{GAIN,PEAK} to the file,
    and removes any R128 gain tags that may be present. The REPLAYGAIN tags
    use the -18 LUFS reference level from ReplayGain 2.0.

    This method is against the spirit of the specifications, but has good
    application compatibility (most applications share parsing code between
    Opus tags and other formats).
    """

    COMPATIBLE = 3
    """
    Write both R128 gain and REPLAYGAIN tags.

    This writes the tags from both the "standard" and "replaygain" modes at
    the same time.

    This method is against the spirit of the specifications, but ensures
    maximum application compatibility. Since the same computed gain value
    is used to generate both sets of tags (adjusted the the appropriate
    different reference levels), it doesn't matter which one the application
    uses - the result will be the same.

    This is the default tagging method.
    """


class ID3Mode(Enum):
    REPLAYGAIN = 1
    """Write REPLAYGAIN tags according to the ReplayGain 2.0 spec."""

    RVA2 = 2
    """Use the ID3v2 RVA2 frames to store ReplayGain information."""

    COMPATIBLE = 3
    """
    Write both REPLAYGAIN tags and RVA2 frames.

    This is the default tagging method.
    """


class Tagger:
    REPLAYGAIN_REF = -18.0  # LUFS
    R128_REF = -23.0  # LUFS

    ogg_opus_mode = OggOpusMode.COMPATIBLE
    id3_mode = ID3Mode.COMPATIBLE

    def __init__(self, filename):
        self.filename = filename
        self.tags = GainInfo()
        self.need_album_update = False
        self.need_track_update = False

    rg_gain_re = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)")

    def parse_rg_gain(self, value):
        m = self.rg_gain_re.match(value)
        if m:
            return self.REPLAYGAIN_REF - float(m.group(1))
        return None

    def format_rg_gain(self, loudness):
        return "{:.2f} dB".format(self.REPLAYGAIN_REF - loudness)

    rg_peak_re = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)")

    def parse_rg_peak(self, value):
        m = self.rg_peak_re.match(value)
        if m:
            return 20.0 * log10(float(m.group(1)))
        return None

    def format_rg_peak(self, peak):
        return "{:.6f}".format(10.0 ** (peak / 20.0))

    opus_gain_re = re.compile(r"^\s*([+-]?\d{1,5})")

    def parse_opus_gain(self, value):
        m = self.opus_gain_re.match(value)
        if m:
            return self.R128_REF - float(m.group(1)) / 256.0

    def format_opus_gain(self, value, context):
        value = int((self.R128_REF - value) * 256.0)
        clipped_value = max(-32768, min(value, 32767))

        if value != clipped_value:
            logger.warning(
                "%s: Clipping Opus R128 %s gain adjustment %.2f dB to %.2f dB",
                self.filename,
                context,
                float(value) / 256,
                float(clipped_value) / 256,
            )
            value = clipped_value

        return "{:d}".format(value)

    def format_rva2_peak(self, peak, context):
        # mutagen expects a floating point value on linear PCM scale
        # with a maximum of 65535/32768
        int_peak = decimal.Decimal.from_float(
            (10.0 ** (peak / 20.0)) * 32768
        ).to_integral_value(decimal.ROUND_HALF_EVEN)

        if int_peak > 65535:
            logger.warning(
                "%s: Clipping RVA2 %s peak %.2f to %.2f",
                self.filename,
                context,
                float(int_peak) / 32768,
                65535.0 / 32768,
            )
            int_peak = 65535

        return float(int_peak) / 32768

    def read_gain_id3(self):
        need_update = False
        have_replaygain = False
        have_rva2 = False

        # Load the standard REPLAYGAIN tags first
        # Case-insensitive matching...
        for tag in self.audio.tags.getall("TXXX"):
            if tag.desc.lower() == "replaygain_track_gain":
                if self.tags.loudness is None:
                    self.tags.loudness = self.parse_rg_gain(tag.text[0])
                have_replaygain = True
                if tag.desc != "REPLAYGAIN_TRACK_GAIN":
                    need_update = True
            elif tag.desc.lower() == "replaygain_track_peak":
                if self.tags.peak is None:
                    self.tags.peak = self.parse_rg_peak(tag.text[0])
                have_replaygain = True
                if tag.desc != "REPLAYGAIN_TRACK_PEAK":
                    need_update = True
            elif tag.desc.lower() == "replaygain_album_gain":
                if self.tags.album_loudness is None:
                    self.tags.album_loudness = self.parse_rg_gain(tag.text[0])
                have_replaygain = True
                if tag.desc != "REPLAYGAIN_ALBUM_GAIN":
                    need_update = True
            elif tag.desc.lower() == "replaygain_album_peak":
                if self.tags.album_peak is None:
                    self.tags.album_peak = self.parse_rg_peak(tag.text[0])
                have_replaygain = True
                if tag.desc != "REPLAYGAIN_ALBUM_GAIN":
                    need_update = True

        # Try loading the legacy RVA2 tags if information is missing
        rva2_t = self.audio.tags.get("RVA2:track")
        if rva2_t is not None and rva2_t.channel == 1:
            if self.tags.loudness is None or self.tags.peak is None:
                self.tags.loudness = self.REPLAYGAIN_REF - rva2_t.gain
                self.tags.peak = 20.0 * log10(rva2_t.peak)
            have_rva2 = True
        rva2_a = self.audio.tags.get("RVA2:album")
        if rva2_a is not None and rva2_a.channel == 1:
            if self.tags.album_loudness is None or self.tags.album_peak is None:
                self.tags.album_loudness = self.REPLAYGAIN_REF - rva2_a.gain
                self.tags.album_peak = 20.0 * log10(rva2_a.peak)
            have_rva2 = True

        if have_rva2 and not (
            self.id3_mode is ID3Mode.RVA2 or self.id3_mode is ID3Mode.COMPATIBLE
        ):
            need_update = True
        if have_replaygain and not (
            self.id3_mode is ID3Mode.REPLAYGAIN or self.id3_mode is ID3Mode.COMPATIBLE
        ):
            need_update = True
        if not have_rva2 and (
            self.id3_mode is ID3Mode.RVA2 or self.id3_mode is ID3Mode.COMPATIBLE
        ):
            need_update = True
        if not have_replaygain and (
            self.id3_mode is ID3Mode.REPLAYGAIN or self.id3_mode is ID3Mode.COMPATIBLE
        ):
            need_update = True

        self.need_track_update = need_update
        self.need_album_update = need_update

        return

    def read_gain_ogg_opus(self):
        need_update = False
        have_r128 = False
        have_replaygain = False

        # Read the opus-specific 'R128' tags
        r128_tg = self.audio.get("R128_TRACK_GAIN")
        if r128_tg is not None:
            if self.tags.loudness is None:
                self.tags.loudness = self.parse_opus_gain(r128_tg[0])
            have_r128 = True
        r128_ag = self.audio.get("R128_ALBUM_GAIN")
        if r128_ag is not None:
            if self.tags.album_loudness is None:
                self.tags.album_loudness = self.parse_opus_gain(r128_ag[0])
            have_r128 = True

        # For compatibility, also read the generic replaygain tags
        rg_tg = self.audio.get("REPLAYGAIN_TRACK_GAIN")
        if rg_tg is not None:
            if self.tags.loudness is None:
                self.tags.loudness = self.parse_rg_gain(rg_tg[0])
            have_replaygain = True
        rg_tp = self.audio.get("REPLAYGAIN_TRACK_PEAK")
        if rg_tp is not None:
            if self.tags.peak is None:
                self.tags.peak = self.parse_rg_peak(rg_tp[0])
            have_replaygain = True
        rg_ag = self.audio.get("REPLAYGAIN_ALBUM_GAIN")
        if rg_ag is not None:
            if self.tags.album_loudness is None:
                self.tags.album_loudness = self.parse_rg_gain(rg_ag[0])
            have_replaygain = True
        rg_ap = self.audio.get("REPLAYGAIN_ALBUM_PEAK")
        if rg_ap is not None:
            if self.tags.album_peak is None:
                self.tags.album_peak = self.parse_rg_peak(rg_ap[0])
            have_replaygain = True

        # This is a hack, R128 gain tags don't store peak, but
        # we want to mark it as valid tag even without the peak
        if have_r128 and self.ogg_opus_mode is OggOpusMode.R128:
            if self.tags.loudness is not None and self.tags.peak is None:
                self.tags.peak = float("nan")
            if self.tags.album_loudness is not None and self.tags.album_peak is None:
                self.tags.album_peak = float("nan")

        if have_r128 and not (
            self.ogg_opus_mode is OggOpusMode.R128
            or self.ogg_opus_mode is OggOpusMode.COMPATIBLE
        ):
            need_update = True
        if have_replaygain and not (
            self.ogg_opus_mode is OggOpusMode.REPLAYGAIN
            or self.ogg_opus_mode is OggOpusMode.COMPATIBLE
        ):
            need_update = True
        if not have_r128 and (
            self.ogg_opus_mode is OggOpusMode.R128
            or self.ogg_opus_mode is OggOpusMode.COMPATIBLE
        ):
            need_update = True
        if not have_replaygain and (
            self.ogg_opus_mode is OggOpusMode.REPLAYGAIN
            or self.ogg_opus_mode is OggOpusMode.COMPATIBLE
        ):
            need_update = True

        self.need_track_update = need_update
        self.need_album_update = need_update

    def read_gain_mp4(self):
        # These are the tags used by foobar2000, and are compatible with
        # rockbox.
        for key, value in self.audio.tags.items():
            atom_name = key[:4]
            if atom_name != "----":
                continue

            _, mean, name = key.split(":", 2)

            if not (
                mean == "com.apple.iTunes" or mean == "org.hydrogenaudio.replaygain"
            ):
                continue

            if value[0].dataformat == mutagen.mp4.AtomDataType.UTF8:
                value = value[0].decode(encoding="UTF-8")
            else:
                continue

            name = name.lower()
            if name == "replaygain_track_gain":
                if self.tags.loudness is None:
                    self.tags.loudness = self.parse_rg_gain(value)
                continue
            if name == "replaygain_track_peak":
                if self.tags.peak is None:
                    self.tags.peak = self.parse_rg_peak(value)
                continue
            if name == "replaygain_album_gain":
                if self.tags.album_loudness is None:
                    self.tags.album_loudness = self.parse_rg_gain(value)
                continue
            if name == "replaygain_album_peak":
                if self.tags.album_peak is None:
                    self.tags.album_peak = self.parse_rg_peak(value)
                continue

        return self.tags

    def read_gain_generic(self):
        rg_tg = self.audio.get("REPLAYGAIN_TRACK_GAIN")
        if rg_tg is not None:
            if self.tags.loudness is None:
                self.tags.loudness = self.parse_rg_gain(rg_tg[0])
        rg_tp = self.audio.get("REPLAYGAIN_TRACK_PEAK")
        if rg_tp is not None:
            if self.tags.peak is None:
                self.tags.peak = self.parse_rg_peak(rg_tp[0])
        rg_ag = self.audio.get("REPLAYGAIN_ALBUM_GAIN")
        if rg_ag is not None:
            if self.tags.album_loudness is None:
                self.tags.album_loudness = self.parse_rg_gain(rg_ag[0])
        rg_ap = self.audio.get("REPLAYGAIN_ALBUM_PEAK")
        if rg_ap is not None:
            if self.tags.album_peak is None:
                self.tags.album_peak = self.parse_rg_peak(rg_ap[0])

    def read_gain(self):
        self.need_track_update = False
        self.need_album_update = False
        self.audio = mutagen.File(self.filename)

        if isinstance(self.audio, mutagen.id3.ID3FileType) and self.audio.tags is None:
            self.audio.add_tags()

        if self.audio is None or self.audio.tags is None:
            raise Exception(
                "Unable to determine tag format for file: {}".format(self.filename)
            )

        if isinstance(self.audio.tags, mutagen.id3.ID3):
            self.read_gain_id3()
        elif isinstance(self.audio, mutagen.oggopus.OggOpus):
            self.read_gain_ogg_opus()
        elif isinstance(self.audio, mutagen.mp4.MP4):
            self.read_gain_mp4()
        else:
            self.read_gain_generic()

        if self.tags.album_loudness is not None or self.tags.album_peak is not None:
            self.need_track_update = True

        return self.tags

    def write_gain_id3(self):
        logger.debug("Writing ID3 tags using mode %s", self.id3_mode.name)
        # Delete standard ReplayGain tags
        to_delete = []
        for tag in self.audio.tags.getall("TXXX"):
            name = tag.desc.lower()
            if (
                name == "replaygain_track_gain"
                or name == "replaygain_track_peak"
                or name == "replaygain_album_gain"
                or name == "replaygain_album_peak"
                or name == "replaygain_reference_loudness"
            ):
                to_delete.append(tag.HashKey)
        for key in to_delete:
            logger.debug("Removing %s", key)
            del self.audio.tags[key]
        # Delete RVA2 frames
        if "RVA2:track" in self.audio:
            logger.debug("Removing RVA2:track")
            del self.audio["RVA2:track"]
        if "RVA2:album" in self.audio:
            logger.debug("Removing RVA2:album")
            del self.audio["RVA2:album"]

        if self.id3_mode is ID3Mode.REPLAYGAIN or self.id3_mode is ID3Mode.COMPATIBLE:
            if self.tags.loudness is not None:
                gain = self.format_rg_gain(self.tags.loudness)
                logger.debug("Adding TXXX:REPLAYGAIN_TRACK_GAIN=%s", gain)
                self.audio.tags.add(
                    mutagen.id3.TXXX(
                        encoding=0, desc="REPLAYGAIN_TRACK_GAIN", text=[gain]
                    )
                )
            if self.tags.peak is not None:
                peak = self.format_rg_peak(self.tags.peak)
                logger.debug("Adding TXXX:REPLAYGAIN_TRACK_PEAK=%s", peak)
                self.audio.tags.add(
                    mutagen.id3.TXXX(
                        encoding=0, desc="REPLAYGAIN_TRACK_PEAK", text=[peak]
                    )
                )
            if self.tags.album_loudness is not None:
                gain = self.format_rg_gain(self.tags.album_loudness)
                logger.debug("Adding TXXX:REPLAYGAIN_ALBUM_GAIN=%s", gain)
                self.audio.tags.add(
                    mutagen.id3.TXXX(
                        encoding=0, desc="REPLAYGAIN_ALBUM_GAIN", text=[gain]
                    )
                )
            if self.tags.album_peak is not None:
                peak = self.format_rg_peak(self.tags.album_peak)
                logger.debug("Adding TXXX:REPLAYGAIN_ALBUM_PEAK=%s", peak)
                self.audio.tags.add(
                    mutagen.id3.TXXX(
                        encoding=0, desc="REPLAYGAIN_ALBUM_PEAK", text=[peak]
                    )
                )

        if self.id3_mode is ID3Mode.RVA2 or self.id3_mode is ID3Mode.COMPATIBLE:
            if self.tags.loudness is not None and self.tags.peak is not None:
                gain = self.REPLAYGAIN_REF - self.tags.loudness
                peak = self.format_rva2_peak(self.tags.peak, "track")
                logger.debug(
                    "Adding RVA2:track={channel=1, gain=%f, peak=%f}", gain, peak
                )
                self.audio.tags.add(
                    mutagen.id3.RVA2(desc="track", channel=1, gain=gain, peak=peak)
                )
            if (
                self.tags.album_loudness is not None
                and self.tags.album_peak is not None
            ):
                gain = self.REPLAYGAIN_REF - self.tags.album_loudness
                peak = self.format_rva2_peak(self.tags.album_peak, "album")
                logger.debug(
                    "Adding RVA2:album={channel=1, gain=%f, peak=%f}", gain, peak
                )
                self.audio.tags.add(
                    mutagen.id3.RVA2(desc="album", channel=1, gain=gain, peak=peak)
                )

        self.audio.tags.update_to_v24()
        self.audio.save()

    def write_gain_ogg_opus(self):
        # Delete all tags, particularly needed if switching modes
        self.write_gain_generic_cleanup()

        if (
            self.ogg_opus_mode is OggOpusMode.R128
            or self.ogg_opus_mode is OggOpusMode.COMPATIBLE
        ):
            if self.tags.loudness is not None:
                self.audio["R128_TRACK_GAIN"] = [
                    self.format_opus_gain(self.tags.loudness, "track")
                ]
            if self.tags.album_loudness is not None:
                self.audio["R128_ALBUM_GAIN"] = [
                    self.format_opus_gain(self.tags.album_loudness, "album")
                ]

        if (
            self.ogg_opus_mode is OggOpusMode.REPLAYGAIN
            or self.ogg_opus_mode is OggOpusMode.COMPATIBLE
        ):
            if self.tags.loudness is not None:
                self.audio["REPLAYGAIN_TRACK_GAIN"] = [
                    self.format_rg_gain(self.tags.loudness)
                ]
            if self.tags.peak is not None:
                self.audio["REPLAYGAIN_TRACK_PEAK"] = [
                    self.format_rg_peak(self.tags.peak)
                ]
            if self.tags.album_loudness is not None:
                self.audio["REPLAYGAIN_ALBUM_GAIN"] = [
                    self.format_rg_gain(self.tags.album_loudness)
                ]
            if self.tags.album_peak is not None:
                self.audio["REPLAYGAIN_ALBUM_PEAK"] = [
                    self.format_rg_peak(self.tags.album_peak)
                ]

        self.audio.save()

    def write_gain_mp4(self):
        to_delete = []
        for key, value in self.audio.tags.items():
            atom_name = key[:4]
            if atom_name != "----":
                continue

            _, mean, name = key.split(":", 2)

            if not (
                mean == "com.apple.iTunes" or mean == "org.hydrogenaudio.replaygain"
            ):
                continue

            if value[0].dataformat == mutagen.mp4.AtomDataType.UTF8:
                value = value[0].decode(encoding="UTF-8")
            else:
                continue

            name = name.lower()
            if (
                name == "replaygain_track_gain"
                or name == "replaygain_track_peak"
                or name == "replaygain_album_gain"
                or name == "replaygain_album_peak"
            ):
                to_delete.append(key)
        for key in to_delete:
            del self.audio.tags[key]

        # These are the tags used by foobar2000, and are compatible with
        # rockbox.
        if self.tags.loudness is not None:
            self.audio.tags["----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN"] = [
                mutagen.mp4.MP4FreeForm(
                    self.format_rg_gain(self.tags.loudness).encode(encoding="UTF-8")
                )
            ]
        if self.tags.peak is not None:
            self.audio.tags["----:com.apple.iTunes:REPLAYGAIN_TRACK_PEAK"] = [
                mutagen.mp4.MP4FreeForm(
                    self.format_rg_peak(self.tags.peak).encode(encoding="UTF-8")
                )
            ]
        if self.tags.album_loudness is not None:
            self.audio.tags["----:com.apple.iTunes:REPLAYGAIN_ALBUM_GAIN"] = [
                mutagen.mp4.MP4FreeForm(
                    self.format_rg_gain(self.tags.album_loudness).encode(
                        encoding="UTF-8"
                    )
                )
            ]
        if self.tags.album_peak is not None:
            self.audio.tags["----:com.apple.iTunes:REPLAYGAIN_ALBUM_PEAK"] = [
                mutagen.mp4.MP4FreeForm(
                    self.format_rg_peak(self.tags.album_peak).encode(encoding="UTF-8")
                )
            ]

        self.audio.save()

    def write_gain_generic_cleanup(self):
        # Delete the standard tags
        if "REPLAYGAIN_TRACK_GAIN" in self.audio:
            del self.audio["REPLAYGAIN_TRACK_GAIN"]
        if "REPLAYGAIN_TRACK_PEAK" in self.audio:
            del self.audio["REPLAYGAIN_TRACK_PEAK"]
        if "REPLAYGAIN_ALBUM_GAIN" in self.audio:
            del self.audio["REPLAYGAIN_ALBUM_GAIN"]
        if "REPLAYGAIN_ALBUM_PEAK" in self.audio:
            del self.audio["REPLAYGAIN_ALBUM_PEAK"]
        # Delete unusual/old tags
        if "REPLAYGAIN_REFERENCE_LOUDNESS" in self.audio:
            del self.audio["REPLAYGAIN_REFERENCE_LOUDNESS"]
        # Ogg Opus R128 gain tags (shouldn't ever be in other formats...)
        if "R128_TRACK_GAIN" in self.audio:
            del self.audio["R128_TRACK_GAIN"]
        if "R128_ALBUM_GAIN" in self.audio:
            del self.audio["R128_ALBUM_GAIN"]

    def write_gain_generic(self):
        self.write_gain_generic_cleanup()

        if self.tags.loudness is not None:
            self.audio["REPLAYGAIN_TRACK_GAIN"] = [
                self.format_rg_gain(self.tags.loudness)
            ]
        if self.tags.peak is not None:
            self.audio["REPLAYGAIN_TRACK_PEAK"] = [self.format_rg_peak(self.tags.peak)]
        if self.tags.album_loudness is not None:
            self.audio["REPLAYGAIN_ALBUM_GAIN"] = [
                self.format_rg_gain(self.tags.album_loudness)
            ]
        if self.tags.album_peak is not None:
            self.audio["REPLAYGAIN_ALBUM_PEAK"] = [
                self.format_rg_peak(self.tags.album_peak)
            ]

        self.audio.save()

    def write_gain(self, tags):
        self.tags = tags
        if self.audio is None:
            raise Exception("write_gain called without previous read_gain")
        if isinstance(self.audio.tags, mutagen.id3.ID3):
            return self.write_gain_id3()
        if isinstance(self.audio, mutagen.oggopus.OggOpus):
            return self.write_gain_ogg_opus()
        if isinstance(self.audio, mutagen.mp4.MP4):
            return self.write_gain_mp4()
        return self.write_gain_generic()


class GainScanner:
    i_re = re.compile(r"^\s+I:\s+(-?\d+\.\d+) LUFS$", re.M)
    peak_re = re.compile(r"^\s+Peak:\s+(-?\d+\.\d+) dBFS$", re.M)

    async def ffmpeg_parse_ebur128(self, *ff_opts):
        ff_args = (
            [
                "ffmpeg",
                "-nostats",
                "-nostdin",
                "-hide_banner",
                "-vn",
                "-loglevel",
                "info",
            ]
            + list(ff_opts)
            + ["-f", "null", "-"]
        )
        logger.debug("ffmpeg command: %r", ff_args)
        ffmpeg = await asyncio.create_subprocess_exec(
            *ff_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
        (_, stderr_data) = await ffmpeg.communicate()

        if ffmpeg.returncode != 0:
            raise RuntimeError(stderr_data.decode())

        result = GainInfo()
        for line_bytes in stderr_data.splitlines():
            line_str = line_bytes.decode()

            m = self.i_re.search(line_str)
            if m:
                result.loudness = float(m.group(1))
            m = self.peak_re.search(line_str)
            if m:
                result.peak = float(m.group(1))

        return result

    async def scan_track(self, filename):
        result = await self.ffmpeg_parse_ebur128(
            "-i",
            "file:" + filename,
            "-filter_complex",
            "ebur128=framelog=verbose:peak=true[out]",
            "-map",
            "[out]",
        )
        return result

    async def scan_album(self, filenames):
        if len(filenames) == 0:
            raise ValueError("filenames is empty")
        ff_args = []
        for filename in filenames:
            ff_args += ["-i", "file:" + filename]
        ff_args += [
            "-filter_complex",
            "concat=n={}:v=0:a=1,ebur128=framelog=verbose[out]".format(len(filenames)),
            "-map",
            "[out]",
        ]
        result = await self.ffmpeg_parse_ebur128(*ff_args)
        return GainInfo(album_loudness=result.loudness, album_peak=result.peak)


class Track:
    def __init__(self, filename, job_sem):
        self.filename = filename
        self.job_sem = job_sem
        self.tagger = Tagger(filename)
        self.gain = GainInfo()

    async def read_tags(self):
        async with self.job_sem:
            loop = asyncio.get_event_loop()
            self.gain = await loop.run_in_executor(None, self.tagger.read_gain)

    async def scan_gain(self):
        async with self.job_sem:
            gain_scanner = GainScanner()
            self.gain = await gain_scanner.scan_track(self.filename)
            logger.debug("Track%d:Calculated gain: %r", id(self), self.gain)

    async def write_tags(self):
        async with self.job_sem:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.tagger.write_gain, self.gain)

    async def scan(self, force=False, skip_save=False):
        await self.read_tags()

        need_scan = False
        if self.gain.loudness is None or self.gain.peak is None:
            need_scan = True
        if force:
            need_scan = True

        need_save = self.tagger.need_track_update

        if need_scan:
            await self.scan_gain()
            need_save = True

        if need_save:
            if not skip_save:
                await self.write_tags()

        print()
        print(self.filename)
        print(self.gain)
        if need_scan:
            print("Rescanned loudness")
        if need_save:
            if not skip_save:
                print("Updated tags")
            else:
                print("Needs tag update")


class AlbumTrack(Track):
    def __init__(self, filename, job_sem, exclude):
        super().__init__(filename, job_sem)
        self.exclude = exclude


class Album:
    def __init__(self, album_param, job_sem):
        self.job_sem = job_sem

        self.gain = GainInfo()
        self.tracks = []
        for filename in album_param["track"]:
            self.tracks.append(AlbumTrack(filename, job_sem, exclude=False))
        for filename in album_param["exclude"]:
            self.tracks.append(AlbumTrack(filename, job_sem, exclude=True))

    async def read_tags(self):
        track_tasks = [track.read_tags() for track in self.tracks]
        await asyncio.gather(*track_tasks)

    async def scan_album_gain(self):
        included = [t.filename for t in self.tracks if not t.exclude]
        async with self.job_sem:
            gain_scanner = GainScanner()
            self.gain = await gain_scanner.scan_album(included)

    async def scan_gain(self):
        album_task = asyncio.ensure_future(self.scan_album_gain())
        track_tasks = [track.scan_gain() for track in self.tracks]

        await asyncio.gather(album_task, *track_tasks)

        self.gain.album_peak = max([t.gain.peak for t in self.tracks])
        logger.debug("Album%d:Calculated album gain: %r", id(self), self.gain)
        for track in self.tracks:
            track.gain.album_loudness = self.gain.album_loudness
            track.gain.album_peak = self.gain.album_peak

    async def write_tags(self):
        track_tasks = [track.write_tags() for track in self.tracks]
        await asyncio.gather(*track_tasks)

    async def scan(self, force=False, skip_save=False):
        await self.read_tags()

        need_scan = False
        for track in self.tracks:
            if track.gain.loudness is None or track.gain.peak is None:
                need_scan = True
            if self.gain.album_loudness is None:
                self.gain.album_loudness = track.gain.album_loudness
            if self.gain.album_loudness != track.gain.album_loudness:
                need_scan = True
            if self.gain.album_peak is None:
                self.gain.album_peak = track.gain.album_peak
            if self.gain.album_peak != track.gain.album_peak:
                need_scan = True
        if self.gain.album_loudness is None or self.gain.album_peak is None:
            need_scan = True
        if force:
            need_scan = True

        need_save = any([track.tagger.need_album_update for track in self.tracks])

        if need_scan:
            await self.scan_gain()
            need_save = True

        if need_save:
            if not skip_save:
                await self.write_tags()

        print()
        for track in self.tracks:
            print(track.filename)
            print(track.gain)
        if need_scan:
            print("Rescanned loudness")
        if need_save:
            if not skip_save:
                print("Updated tags")
            else:
                print("Needs tag update")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="""
            Add ReplayGain tags to files using the EBU R128 algorithm.
            """,
        epilog="""
            If neither --track or --album are specified, the mode used depends on
            the number of files given as arguments. If a single file is given, it
            will be processed in track mode. If multiple files are given, they
            will be processed in album mode as a single album.
            """,
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        default=False,
        action="store_true",
        help="""
            Only calculate and display the ReplayGain values; do not actually
            save the tags in the audio files.
            """,
    )
    parser.add_argument(
        "-f",
        "--force",
        default=False,
        action="store_true",
        help="""
            Recalculate the ReplayGain values even if valid tags are already
            present in the files.
            """,
    )
    parser.add_argument(
        "--debug",
        dest="log_level",
        default=logging.WARNING,
        action="store_const",
        const=logging.DEBUG,
        help="""
            Print a bunch of extra debugging output.
            """,
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=multiprocessing.cpu_count(),
        help="""
            The number of operations to run in parallel. The default is
            auto-detected, currently %(default)s.
            """,
    )
    parser.add_argument(
        "-t",
        "--track",
        nargs="+",
        default=deque(),
        metavar="FILE",
        action=TrackAction,
        help="""
            Treat the following audio files as individual tracks.
            """,
    )
    parser.add_argument(
        "-a",
        "--album",
        nargs="+",
        default=deque(),
        metavar="FILE",
        action=AlbumAction,
        help="""
            Treat the following audio files as part of the same album.
            Each time the --album option is specified, it starts a new album.
            """,
    )
    parser.add_argument(
        "-e",
        "--exclude",
        nargs="+",
        default=deque(),
        metavar="FILE",
        action=AlbumAction,
        help="""
            Tag the following files as part of the current album, but do not
            use their audio when calculating the value for the album ReplayGain
            tag.
            """,
    )
    parser.add_argument("FILE", nargs="*", default=[], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    logging.basicConfig(format="%(levelname)s:%(message)s", level=args.log_level)

    logger.debug("Debug logging has been enabled")
    logger.debug("Command line arguments: %r", args)

    # Handle the "loose" arguments, by turning them into tracks or albums
    if len(args.FILE) + len(args.exclude) > 1 or len(args.exclude) > 0:
        # Treat the initial arguments as an album
        args.album.appendleft(
            {"track": deque(args.FILE), "exclude": deque(args.exclude)}
        )
        args.FILE = None
        args.exclude = None
    elif len(args.FILE) > 0:
        args.track.extend(args.FILE)
        args.FILE = None

    if len(args.track) == 0 and len(args.album) == 0:
        parser.print_usage()
        sys.exit(2)

    loop = asyncio.get_event_loop()

    job_sem = asyncio.BoundedSemaphore(args.jobs)

    tasks = []
    albums = [Album(album, job_sem) for album in args.album]
    tasks += [album.scan(force=args.force, skip_save=args.dry_run) for album in albums]
    tracks = [Track(track, job_sem) for track in args.track]
    tasks += [track.scan(force=args.force, skip_save=args.dry_run) for track in tracks]

    future = asyncio.ensure_future(asyncio.gather(*tasks))

    loop.run_until_complete(future)

    future.result()

    loop.close()


if __name__ == "__main__":
    main(sys.argv[1:])
