# regainer is an Advanced ReplayGain scanner and tagger

I wanted a simple-to-use tool that can apply ReplayGain compatible tags to a
wide variety of audio files - even mixing and matching different file types,
sample rates, etc. I couldn’t find any existing tools that did the job, so I
wrote regainer.

regainer writes ReplayGain tags compatible with the
[ReplayGain 2.0](https://wiki.hydrogenaud.io/index.php?title=ReplayGain_2.0_specification)
proposed specification. The tags written by regainer are compatible with a
wide variety of players, even older players. It does this by writing
multiple formats of tags within the same file for file types like
mp3 which have multiple ways to store ReplayGain information.

If you find a player that is not compatible with tags written by regainer,
please let me know by filing an issue on Github.

## Requirements

regainer is written in Python 3. It should work with Python 3.7 and any later
release. In addition to Python, you will need the following:

- [mutagen](https://mutagen.readthedocs.io/), a Python library that can read
  and write tags from a wide variety of audio formats.
- [ffmpeg](https://www.ffmpeg.org/) command line tools, version 4.0 or later.
  I use the “ebur128” filter in ffmpeg to calculate loudness levels.

## Installation

Packaging is still a work in progress. But regainer is a single python script,
so it's sufficient to just copy or symlink it to somewhere in your path. Some
examples:

```bash
sudo cp regainer.py /usr/local/bin/regainer
# or
sudo ln -s /path/to/regainer.py /home/username/bin/regainer
```

## Using regainer

The simplest use case is to add track (aka “radio”) ReplayGain tags to a single
file:

```
$ regainer track.mp3
```

Or you can add both track and album (aka “audiophile”) ReplayGain tags to all
the tracks in an album:

```
$ regainer Album/*.opus
```

You can mix and match different modes by specifying the `-a` and `-t` options.
Using `-a` starts a new album, and using `-t` disables album processing for
the following tracks. You can use `-t` if you want to add only track ReplayGain
tags to several files at the same time.

```
$ regainer -a Album1/*.ogg \
    -t A_Single_track.mp3 A_different_single_track.opus \
    -a Another_album/*.m4a
```

In some cases, you might want to process gain for a complete album, but exclude
certain tracks from being included in the calculated album gain value - for
example, there might be some karaoke song versions, drama or interview tracks,
etc. You can use the -e option to do this.

In this example, the album gain tag will be written to all 4 tracks, but only
tracks 1 and 2 will be used to calculate the album gain:

```
$ regainer -a 01.opus 02.opus -e 03.opus 04.opus
```

Some additional options may also come in handy:

`-n` puts regainer into dry-run mode. It will calculate and print the
loudness levels in the file, but it will not write the ReplayGain tags.

`-f` causes regainer to recalculate the loudness levels even if ReplayGain
tags are already present in the files. If you have files which had loudness
levels calculated using the old ReplayGain algorithm, you can use this option
to recalculate using the EBU R128 algorithm.

`-j N` specifies the number of parallel jobs to run. By default it will
use all CPUs available on the system. You can use this to reduce the amount
of CPU that regainer will use.

## License

regainer is released under the terms of the MIT license; see the file
[COPYING](COPYING) for the license text. This is a very simple non-copyleft
license, but note that the dependencies of regainer may have different licenses.

I encourage developers of music tagging applications to reference or use
regainer code to improve their support for ReplayGain tags.

## Format-specific Notes

### Flac

regainer does not support writing ReplayGain information to an embedded CUE
file.

### M4A (AAC, ALAC, etc.)

regainer uses comment tags in the `com.apple.iTunes` namespace. This
is compatible with the tag format used by foobar2000 and many other players.

Older tags in the `org.hydrogenaudio.replaygain` namespace will be read and
converted to the new format.

regainer does not support writing tags compatible with iTunes Sound Check.

### MP3

regainer currently converts all MP3 ID3v2 tags to version 2.4. This might
cause issues with some players, particularly on Windows. If you are affected,
please file a Github issue and I'll look into having an option to specify
the preferred tag version.

In addition to the standard `TXXX` ReplayGain tags, regainer also writes
ID3v2 `RVA2` tags as specified in the
[Replaygain legacy metadata formats](https://wiki.hydrogenaud.io/index.php?title=ReplayGain_legacy_metadata_formats)
document.

regainer does not read or write the LAME header ReplayGain tags. As far as I
know, no players read this format. Or at least, if any do, they also support
(and prefer) the standard tag formats.

### Ogg Opus

In addition to the standard VorbisComment ReplayGain tags, regainer also
writes the opus-specific `R128_TRACK_GAIN` and `R128_ALBUM_GAIN` tags
specified in the
[Ogg Encapsulation for the Opus Audio Codec](https://tools.ietf.org/html/rfc7845.html)
proposed standard. The correct reference level is used for these tags so that
you music will play back at the same volume regardless of whether the player
reads the ReplayGain or R128 tags.

Note that regainer does not modify the ID header “output gain” field in
Ogg Opus files. Gain values for ReplayGain and the R128 tags are both
applied in *addition* to the ID header output gain field. If you modify the
ID header output gain field, you should re-run regainer with the `-f`
option to recalculate the ReplayGain and R128 header values.

## Additional References

I'm working on a set of
[ReplayGain Test Vectors](https://github.com/kepstin/replaygain-test-vectors)
for various audio formats. This is still a work in progress, but I'd be
interested in hearing from you about players that don't pass the test vectors.
