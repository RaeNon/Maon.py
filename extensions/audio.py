import os.path
import asyncio
import subprocess
import configuration as config
from discord.ext import commands
from extensions.player import audioplayer
from youtube_dl import YoutubeDL
from youtube_dl.utils import DownloadError
from tinytag import TinyTag, TinyTagException
from time import sleep
from pathlib import Path
from os import listdir


class Audio(commands.Cog):
    __slots__ = ["client", "players", "cached_songs", "running",
                "info_queue", "info_task", "download_queue", "download_task", "cache_queue",
                "cache_task", "track_queue", "track_task"]

    def __init__(self, client):
        self.client = client
        self.players = {}
        self.cached_songs = {}
        self.running = True
        self.info_queue = asyncio.Queue()
        self.info_task = self.client.loop.create_task(self.info_loop())
        self.download_queue = asyncio.Queue()
        self.download_task = self.client.loop.create_task(self.download_loop())
        self.cache_queue = asyncio.Queue()
        self.cache_task = self.client.loop.create_task(self.cache_loop())
        self.track_queue = asyncio.Queue()
        self.track_task = self.client.loop.create_task(self.track_loop())

    async def prep_link_track(self, message, url: str):
        """
        Looks up the requested url in the cached_songs dictionary to see if the track exists in the
        temp folder. If not, starts the download / streaming process.
        """
        # All we know at this point is the url and who requested it. Get the video_id from the url.
        video_id = await get_video_id(url)
        if video_id is None:
            return await message.send("That link looks invalid to me.")
        
        # Look up video_id in cached_songs dictionary and build a track if an entry exists.
        filename = self.cached_songs.get(video_id)
        if filename is not None:
            track_title = filename[:len(filename) - 16]
            track_url = "./temp/" + filename
            track = {"title": track_title, "url": track_url, "track_type": "music", "message": message}

            await self.track_queue.put(track)
        
        else: # Track is not in temp folder, hand it over to info_queue and start the downloading or streaming process.
            req = {"message": message, "url": url, "video_id": video_id}
            return await self.info_queue.put(req)


    async def prep_local_track(self, message, url: str):
        """ Build local track information for the audioplayer """
        tag = TinyTag.get(config.MUSIC_PATH + url)
        if tag.title is None:
            tag.title = url
        track = {"title": tag.title, "url": config.MUSIC_PATH + url, "track_type": "music", "message": message}
        return await self.track_queue.put(track)


    async def track_loop(self):
        """ Centralize the queuing of tracks in this task """
        try:
            while self.running:
                track = await self.track_queue.get()
                message = track.get("message")

                if message.guild.id not in self.players:
                    self.players[message.guild.id] = audioplayer.AudioPlayer(self.client, message)
                await self.players[message.guild.id].queue.put(track)
                if message.guild.voice_client.is_playing():
                    await message.channel.send("{} has been added to the queue.".format(track.get("title")), delete_after=20)
        
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


    async def info_loop(self):
        """ Gather information about the requested song and process it """
        try:
            while self.running:
                req = await self.info_queue.get()
                message = req.get("message")
                # Find out if the video is a normal video or live stream.
                # If it's a video longer than 15 minutes, maybe also stream it?
                video_info = {}
                try:
                    video_info = await self.client.loop.run_in_executor(
                        None, lambda: YoutubeDL(config.YTDL_INFO_OPTIONS).extract_info(req["url"], download=False))
                    await message.channel.send("Preparing {}...".format(video_info.get("title")))
                except DownloadError:
                    await message.channel.send("I could not download the video's meta data... maybe try again in a few seconds.")
                    continue

                if video_info.get("protocol"):
                    # It's a live stream
                    await message.channel.send("Live streams are not supported yet.")
                    continue
                else:
                    # It's a normal video
                    # Find out if the video is 15 minutes or shorter (maybe make this one configurable)
                    #if video_info.get("duration") < 900:

                    # Get the best audio format id and attach it to the request
                    formats = video_info.get("formats", [video_info])
                    format_ids = []
                    for f in formats:
                        format_ids.append(f['format_id'])
                    if "251" in format_ids:
                        req["format_id"] = "251"
                    else:
                        req["format_id"] = "140"

                    await self.download_queue.put(req)

        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    async def download_loop(self):
        """ Downloads a requested song and stores it in the temp folder for ease of access and replayability """ 
        try:
            while self.running:
                req = await self.download_queue.get()
                message = req.get("message")

                command = config.AUDIO_DOWNLOAD_CMD_DEFAULT
                command[10] = req.get("format_id")
                command[11] = req.get("url")

                result = await self.client.loop.run_in_executor(
                    None, lambda: subprocess.run(command, stdout=subprocess.PIPE).returncode)
                if result == 0:
                    await self.cache_queue.put(req)
                else:
                    await message.channel.send("I ran into an error during download... maybe try again in a few seconds.")

        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    async def cache_loop(self):
        """ Keeps track of files in the temp folder and cleans it up if necessary """
        # Load the cache first
        temp_list = {}
        try:
            temp_list = listdir("./temp/")
            for filename in temp_list:
                if filename.endswith(".mp3"):
                    video_id = filename[len(filename) - 15 : len(filename) - 4]
                    self.cached_songs[video_id] = filename
        except FileNotFoundError:
            print("[Audio] The temp folder does not exist, skipped loading the cache.")

        try:
            while self.running:
                req = await self.cache_queue.get()
                video_id = req.get("video_id")
                
                # Find file by video_id because the ytdl library filters chars out, title != filename
                temp_list = listdir("./temp/")
                for filename in temp_list:
                    if filename.endswith(video_id + ".mp3"):
                        self.cached_songs[video_id] = filename
                        track_title = filename[:len(filename) - 16]
                        track_url = "./temp/" + filename

                track = {"title": track_title, "url": track_url, "track_type": "music", "message": req.get("message")}

                # Throw the track into the queue of the audioplayer
                await self.track_queue.put(track)

        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


    # Used invocations:
    # play p sfx s volume v vol j next ...
    # ═══ Commands ═════════════════════════════════════════════════════════════════════════════════════════════════════
    @commands.command(aliases=["p"])
    @commands.guild_only()
    async def play(self, message, *, url: str = None):
        if not await check_voice_state(message):
            return

        if url is None:
            return await message.send(
                "You can browse the music folder with `browse music`, if you're looking for something specific.")
        elif url.startswith("https://www.youtube.com/") or url.startswith("https://youtu.be/") or url.startswith("https://m.youtube.com/"):
            await self.prep_link_track(message, url)
        elif os.path.exists(config.MUSIC_PATH + url + ".mp3"):
            await self.prep_local_track(message, url + ".mp3")
        elif os.path.exists(config.MUSIC_PATH + url + ".wav"):
            await self.prep_local_track(message, url + ".wav")
        else:
            return await message.send("I need a Youtube link or file path to play.")

    async def fb_play(self, message, url):
        try:
            tag = TinyTag.get(url)
        except TinyTagException:
            return
        
        if not await check_voice_state(message):
            return
        
        if tag.title is None:
            tag.title = url[url.rfind("/") + 1:]
        track = {"title": tag.title, "url": url, "track_type": "music"}

        if message.guild.id not in self.players:
            self.players[message.guild.id] = audioplayer.AudioPlayer(self.client, message)
        await self.players[message.guild.id].queue.put(track)
        if message.guild.voice_client.is_playing():
            return await message.send("{} has been added to the queue.".format(track.get("title")), delete_after=20)

    @commands.command(aliases=["s"])
    @commands.guild_only()
    async def sfx(self, message, *, url: str = None):
        track = {}
        if url is None:
            return await message.send(
                "You can browse the sfx folder with `browse sfx`, if you're looking for something specific.")
        elif os.path.exists(config.SFX_PATH + url + ".mp3"):
            track["url"] = config.SFX_PATH + url + ".mp3"
        elif os.path.exists(config.SFX_PATH + url + ".wav"):
            track["url"] = config.SFX_PATH + url + ".wav"
        else:
            return await message.send("Couldn't find the sound effect you were looking for...")

        track["title"] = url
        track["track_type"] = "sfx"  # link / music / sfx

        if not await check_voice_state(message):
            return

        if message.guild.id not in self.players:
            self.players[message.guild.id] = audioplayer.AudioPlayer(self.client, message)
        await self.players[message.guild.id].queue.put(track)

    async def fb_sfx(self, message, url):
        try:
            tag = TinyTag.get(url)
        except TinyTagException:
            return

        if not await check_voice_state(message):
            return

        if tag.title is None:
            tag.title = url[url.rfind("/") + 1:]
        track = {"title": tag.title, "url": url, "track_type": "sfx"}

        if message.guild.id not in self.players:
            self.players[message.guild.id] = audioplayer.AudioPlayer(self.client, message)
        await self.players[message.guild.id].queue.put(track)

    @commands.command(aliases=["v", "vol"])
    @commands.guild_only()
    async def volume(self, message, *, vol=None):
        if message.author.voice is None:
            return await message.send("You're not in a voice channel, silly.")
        elif message.author.voice.channel != message.guild.voice_client.channel:
            return await message.send("You're not in the voice channel, silly.")
        elif message.guild.voice_client is None:
            return await message.send("I'm not even playing anything. :eyes:")
        elif message.guild.id not in self.players:
            return await message.send("I'm not even playing anything. :eyes:")
        elif vol is None:
            return await message.send("The volume is set to {}%.".format(
                int(self.players[message.guild.id].volume * 100)))

        if not await check_volume(vol):
            return await message.send("Please enter a number ranging from 0 to 100.")

        await volume_gradient(self.players[message.guild.id], message, int(vol))

    @commands.command(aliases=["j"])
    @commands.guild_only()
    async def join(self, message):
        if message.guild.voice_client is None:
            if message.author.voice:
                return await message.author.voice.channel.connect()
            else:
                return await message.send("You're not in a voice channel, silly. :eyes:")
        elif message.author.voice.channel != message.guild.voice_client.channel:
            return await message.guild.voice_client.move_to(message.author.voice.channel)

    @commands.command(aliases=["next"])
    @commands.guild_only()
    async def skip(self, message):
        if not message.guild.voice_client or not message.guild.voice_client.is_connected():
            return await message.send("I'm not playing anything. :eyes:")
        elif message.author.voice is None:
            return await message.send("You're not in a voice channel~")
        elif message.author.voice.channel != message.guild.voice_client.channel:
            return await message.send("I'm not taking orders from someone outside of our voice channel.")
        elif not message.guild.voice_client.is_playing():
            return
        else:
            message.guild.voice_client.stop()
            return await message.send(":track_next: Skipping...")

    @commands.command()
    @commands.guild_only()
    async def loop(self, message, *, option: str = None):
        if not message.guild.voice_client or not message.guild.voice_client.is_connected():
            return await message.send("I'm not playing anything. :eyes:")
        elif message.guild.id not in self.players:
            return await message.send("I'm not playing anything. :eyes:")
        elif message.author.voice is None:
            return await message.send("You're not in a voice channel~")
        elif message.author.voice.channel != message.guild.voice_client.channel:
            return await message.send("I'm not taking orders from someone outside of our voice channel.")

        if option is None:
            return await message.send(
                "Do you want me to loop the `playlist`/`queue`/`q`, the `song`, or turn it `off`?")
        option = option.lower()
        if option == self.players[message.guild.id].looping:
            return await message.send("Looping is currently set to `{}`.".format(option))
        elif option == "off":
            self.players[message.guild.id].looping = "off"
            return await message.send(":repeat_one: I've turned off looping.")
        elif option == "song":
            self.players[message.guild.id].looping = "song"
            return await message.send(":repeat: I'm looping this song now.")
        elif (option == "playlist") or (option == "queue") or (option == "q"):
            self.players[message.guild.id].looping = "playlist"
            return await message.send(":repeat: I'm looping the playlist now.")
        else:
            return await message.send(
                "Do you want me to loop the `playlist`/`queue`/`q`, the `song`, or turn it `off`?")

    @commands.command()
    @commands.guild_only()
    async def pause(self, message):
        if not message.guild.voice_client or not message.guild.voice_client.is_connected():
            return await message.send("I'm not playing anything. :eyes:")
        elif message.author.voice is None:
            return await message.send("You're not in a voice channel~")
        elif message.author.voice.channel != message.guild.voice_client.channel:
            return await message.send("I'm not taking orders from someone outside of our voice channel.")
        elif not message.guild.voice_client.is_playing():
            return
        else:
            message.guild.voice_client.pause()
            return await message.send(":pause_button: Paused.")

    @commands.command(aliases=["res", "cont", "continue"])
    @commands.guild_only()
    async def resume(self, message):
        if not message.guild.voice_client or not message.guild.voice_client.is_connected():
            return await message.send("I'm not playing anything. :eyes:")
        elif message.author.voice is None:
            return await message.send("You're not in a voice channel~")
        elif message.author.voice.channel != message.guild.voice_client.channel:
            return await message.send("I'm not taking orders from someone outside of our voice channel.")
        elif message.guild.voice_client.is_playing():
            return
        elif message.guild.id in self.players:
            message.guild.voice_client.resume()
            return await message.send(":arrow_forward: Continuing...")

    @commands.command(aliases=["leave", "l"])
    @commands.guild_only()
    async def stop(self, message):
        if message.author.voice is None:
            return await message.send("Don't tell me what to do. :eyes:")
        elif not message.guild.voice_client or not message.guild.voice_client.is_connected():
            return await message.send("I'm not even playing anything...")
        elif message.author.voice.channel != message.guild.voice_client.channel:
            return await message.send("Come in here first.")
        else:
            if message.guild.id in self.players:
                self.players[message.guild.id].player_task.cancel()
            else:
                await message.guild.voice_client.disconnect()

    @commands.command()
    @commands.is_owner()
    async def download_depr(self, message, media_type:str = None, *, url:str = None):
        """
        DEPRECATED
        
        This be gone and switched out with a better method when I gots the motivation
        """
        if media_type is None:
            return await message.send("Do you want me to download a full `video` or just the `audio`?")
        elif url is None:
            return await message.send("I need a Youtube link to download from.")
        elif not url.startswith("https://www.youtube.com/") and not url.startswith("https://youtu.be/"):
            return await message.send("I need a Youtube link to download from.")
        elif (media_type != "audio") and (media_type != "video"):
            return await message.send("Do you want me to download a full `video` or just the `audio`?")

        #youtube_feed = etree.HTML(request.urlopen(url).read())
        #title = "".join(youtube_feed.xpath("//span[@id='eow-title']/@title"))
        title = ""

        await message.send("Downloading {}...".format(title))
        if media_type == "audio":
            print("[{}|{}] Downloading audio {}...".format(message.guild.name, message.guild.id, title))
            YoutubeDL(config.YTDL_DOWNLOAD_AUDIO_OPTIONS).download([url])
            print("[{}|{}] Finished downloading audio {}.".format(message.guild.name, message.guild.id, title))
        else:
            print("[{}|{}] Downloading video {}...".format(message.guild.name, message.guild.id, title))
            YoutubeDL(config.YTDL_DOWNLOAD_VIDEO_OPTIONS).download([url])
            print("[{}|{}] Finished downloading video {}.".format(message.guild.name, message.guild.id, title))
        return await message.send("Finished downloading {}!".format(title))

    # ═══ Events ═══════════════════════════════════════════════════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild.id in self.players:
            if message.channel == self.players[message.guild.id].message.channel:
                if os.path.exists(config.SFX_PATH + message.content + ".mp3"):
                    return await self.fb_sfx(message, config.SFX_PATH + message.content + ".mp3")
                elif os.path.exists(config.SFX_PATH + message.content + ".wav"):
                    return await self.fb_sfx(message, config.SFX_PATH + message.content + ".wav")

    # ═══ Helper Methods ═══════════════════════════════════════════════════════════════════════════════════════════════
    def destroy_player(self, message):
        if message.guild.id in self.players:
            del self.players[message.guild.id]
            print("[{}|{}] Audioplayer destroyed.".format(message.guild.name, message.guild.id))


# ═══ Functions ════════════════════════════════════════════════════════════════════════════════════════════════════════
async def volume_gradient(player, message, vol):
    vol_old = int(message.guild.voice_client.source.volume * 100)
    if vol == 0:
        while vol_old > 0:
            vol_old -= 1
            message.guild.voice_client.source.volume = (vol_old / 100)
            sleep(0.010)
        player.volume = 0
        return await message.send("Okay, I'm playing quietly for myself then...")
    elif vol < vol_old:
        while vol_old > vol:
            vol_old -= 1
            message.guild.voice_client.source.volume = (vol_old / 100)
            sleep(0.010)
        player.volume = vol / 100
        return await message.send(":arrow_down_small: I've set the volume to {}%.".format(vol))
    else:
        while vol_old < vol:
            vol_old += 1
            message.guild.voice_client.source.volume = (vol_old / 100)
            sleep(0.010)
        player.volume = vol / 100
        return await message.send(":arrow_up_small: I've set the volume to {}%.".format(vol))


async def check_volume(vol):
    try:
        vol = int(vol)
        if (-1 < vol) and (vol < 101):
            return True
        else:
            return False
    except (TypeError, ValueError):
        return False


async def check_voice_state(message):
    if message.guild.voice_client is None:
        if message.author.voice:
            await message.author.voice.channel.connect()
            return True
        else:
            await message.send("You're not in a voice channel, silly. :eyes:")
            return False
    elif message.author.voice.channel != message.guild.voice_client.channel:
        await message.send("Come in here if you want me to play something. :eyes:")
        return False
    return True


async def prepare_local_track(url: str):
    """ DEPRECATED """
    tag = TinyTag.get(config.MUSIC_PATH + url)
    if tag.title is None:
        tag.title = url
    track = {"title": tag.title, "url": config.MUSIC_PATH + url, "track_type": "music"}
    return track


async def get_video_id(url: str):
    """ Get the 11 chars long video id from a Youtube link """
    if url.find("?v=") > 0:
        return url[url.find("?v=") + 3 : url.find("?v=") + 14] 
    elif url.find("&v=") > 0:
        return url[url.find("&v=") + 3 : url.find("&v=") + 14]
    elif url.find(".be/") > 0:
        return url[url.find(".be/") + 4 : url.find(".be/") + 15]
    else:
        return None


# ═══ Cog Setup ════════════════════════════════════════════════════════════════════════════════════════════════════════
def setup(client):
    client.add_cog(Audio(client))


def teardown(client):
    client.remove_cog(Audio)
