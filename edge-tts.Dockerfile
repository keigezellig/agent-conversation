# Microsoft Edge's neural voices behind an OpenAI-compatible endpoint, for languages the local
# engines have no voice for at all (Serbian, Croatian, Bosnian, Albanian, and 300-odd others).
#
# The published image carries no ffmpeg, and without it the server answers every response_format
# with mp3 while labelling it as whatever was asked for — which the runner would play as noise.
# Adding ffmpeg makes `wav` a real WAV file, so run the conversation with TTS_FORMAT=wav. Asking
# this server for `pcm` fails even with ffmpeg: it builds an invalid command for that format.
#
# Microsoft also turns requests down at random — about one in three while this was measured — and
# refuses outright once the edge-tts client version it reports falls out of favour, so the client is
# upgraded past the version the image pins. Rebuild this image (`--build`) when 403s become constant.
FROM travisvn/openai-edge-tts:latest
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade edge-tts
