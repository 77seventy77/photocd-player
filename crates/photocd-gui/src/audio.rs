//! CD-DA audio playback from .bin files using rodio.
//!
//! Redbook CD-DA: 44100 Hz, 16-bit signed little-endian stereo PCM,
//! 2352 bytes per sector (588 stereo samples).
//!
//! Strategy: read the raw PCM bytes from the .bin slice into a Vec<i16>
//! and hand it to rodio as a `SamplesBuffer`. This bypasses WAV parsing
//! entirely and mirrors how the Python build loads the whole stream into
//! memory before handing it to the audio backend.

use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::Path;

use rodio::buffer::SamplesBuffer;
use rodio::{OutputStream, OutputStreamHandle, Sink};

/// Raw CD-DA sector size in bytes.
const SECTOR_SIZE: u64 = 2352;
const SAMPLE_RATE: u32 = 44100;
const CHANNELS: u16 = 2;

/// Read `pcm_bytes` of little-endian i16 samples starting at `start_byte`
/// in the given .bin file.
fn load_pcm_samples(bin_path: &Path, start_byte: u64, pcm_bytes: u64) -> io::Result<Vec<i16>> {
    let mut file = File::open(bin_path)?;
    file.seek(SeekFrom::Start(start_byte))?;

    // Round down to an even number of bytes (i16 samples).
    let byte_len = (pcm_bytes & !1) as usize;
    let mut raw = vec![0u8; byte_len];
    file.read_exact(&mut raw)?;

    let mut samples = Vec::with_capacity(byte_len / 2);
    for chunk in raw.chunks_exact(2) {
        samples.push(i16::from_le_bytes([chunk[0], chunk[1]]));
    }
    Ok(samples)
}

/// Audio player that plays CD-DA from .bin files.
pub struct AudioPlayer {
    _stream: Option<OutputStream>,
    _stream_handle: Option<OutputStreamHandle>,
    sink: Option<Sink>,
}

/// Describes one audio track to play.
pub struct AudioTrackInfo<'a> {
    pub bin_path: &'a Path,
    pub start_sector: u32,
    pub duration_sectors: u32,
}

impl AudioPlayer {
    pub fn new() -> Self {
        Self {
            _stream: None,
            _stream_handle: None,
            sink: None,
        }
    }

    /// Play multiple audio tracks back-to-back, with an optional seek offset
    /// into the first track (from playlist CDDA entry).
    pub fn play_chained(&mut self, tracks: &[AudioTrackInfo], start_offset_s: f64) {
        self.stop();

        if tracks.is_empty() {
            return;
        }

        let (stream, handle) = match OutputStream::try_default() {
            Ok(pair) => pair,
            Err(e) => {
                eprintln!("Audio output error: {e}");
                return;
            }
        };

        let sink = match Sink::try_new(&handle) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("Audio sink error: {e}");
                return;
            }
        };

        for (i, track) in tracks.iter().enumerate() {
            let start_byte = track.start_sector as u64 * SECTOR_SIZE;
            let total_bytes = track.duration_sectors as u64 * SECTOR_SIZE;

            // Apply start offset only to the first track.
            let (actual_start, actual_len) = if i == 0 && start_offset_s > 0.0 {
                let skip_bytes = ((start_offset_s * 44100.0 * 4.0) as u64).min(total_bytes);
                // Keep alignment on a 4-byte stereo frame boundary.
                let skip_bytes = skip_bytes & !3;
                (start_byte + skip_bytes, total_bytes - skip_bytes)
            } else {
                (start_byte, total_bytes)
            };

            let samples = match load_pcm_samples(track.bin_path, actual_start, actual_len) {
                Ok(s) => s,
                Err(e) => {
                    eprintln!(
                        "Audio: skipping track {} ({}): {e}",
                        i + 1,
                        track.bin_path.display()
                    );
                    continue;
                }
            };

            let source = SamplesBuffer::new(CHANNELS, SAMPLE_RATE, samples);
            sink.append(source);
        }

        sink.play();
        self.sink = Some(sink);
        self._stream_handle = Some(handle);
        self._stream = Some(stream);
    }

    pub fn stop(&mut self) {
        if let Some(sink) = self.sink.take() {
            sink.stop();
        }
        self._stream_handle = None;
        self._stream = None;
    }

    pub fn set_volume(&mut self, volume: f32) {
        if let Some(sink) = &self.sink {
            sink.set_volume(volume.clamp(0.0, 1.0));
        }
    }

    #[allow(dead_code)]
    pub fn is_playing(&self) -> bool {
        self.sink.as_ref().map_or(false, |s| !s.empty())
    }
}

impl Drop for AudioPlayer {
    fn drop(&mut self) {
        self.stop();
    }
}
