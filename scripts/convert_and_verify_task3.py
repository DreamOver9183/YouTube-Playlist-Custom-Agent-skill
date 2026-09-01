import os
import re
import json
import sys

def parse_markdown(md_path):
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    lines = content.splitlines()

    # 1. Document Title and description
    doc_title = lines[0].lstrip('#').strip()
    doc_note = ""
    for line in lines[1:5]:
        if line.startswith('>'):
            doc_note = line.lstrip('>').strip()
            break

    # 2. Overview
    overview = {}
    for line in lines:
        if line.startswith('- **播放清單名稱**：'):
            overview['playlist_name'] = line.split('：')[1].strip(' `')
        elif line.startswith('- **播放清單 ID**：'):
            overview['playlist_id'] = line.split('：')[1].strip(' `')
        elif line.startswith('- **清單連結**：'):
            yt_m = re.search(r'\[YouTube\]\((https?://[^\)]+)\)', line)
            ytm_m = re.search(r'\[YouTube Music\]\((https?://[^\)]+)\)', line)
            if yt_m: overview['playlist_url'] = yt_m.group(1)
            if ytm_m: overview['playlist_music_url'] = ytm_m.group(1)
        elif line.startswith('- **總曲目數**：'):
            m = re.search(r'`(\d+)`\s*首', line)
            if m:
                overview['total_tracks'] = int(m.group(1))
                overview['total_tracks_display'] = f"{m.group(1)} 首"
        elif line.startswith('- **總播放時長**：'):
            m = re.search(r'`([^`]+)`\s*\(([^)]+)\)', line)
            if m:
                overview['total_duration'] = m.group(1)
                overview['total_duration_display'] = m.group(2)
        elif line.startswith('- **累計總觀看次數**：'):
            m = re.search(r'`([^`]+)`\s*次', line)
            if m:
                overview['total_views_formatted'] = m.group(1)
                overview['total_views'] = int(m.group(1).replace(',', ''))
        elif line.startswith('- **累計總按讚數**：'):
            m = re.search(r'`([^`]+)`\s*次', line)
            if m:
                overview['total_likes_formatted'] = m.group(1)
                overview['total_likes'] = int(m.group(1).replace(',', ''))
        elif line.startswith('- **私人／無效影片數**：'):
            m = re.search(r'`(\d+)`\s*首', line)
            if m:
                overview['private_or_invalid_videos'] = int(m.group(1))
                overview['private_or_invalid_videos_display'] = f"{m.group(1)} 首"
        elif line.startswith('- **資料擷取時間**：'):
            m = re.search(r'`([^`]+)`', line)
            if m:
                overview['fetched_at'] = m.group(1)

    # 3. Top 10 Channels
    top_channels = []
    in_top_channels = False
    for line in lines:
        if '### 🏆 主要頻道／歌手分佈' in line:
            in_top_channels = True
            continue
        if in_top_channels:
            if line.startswith('---') or line.startswith('## 🎵'):
                in_top_channels = False
                break
            if line.startswith('|') and not line.startswith('| 頻道') and not line.startswith('|---'):
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) == 3:
                    ch_name = parts[0]
                    count_str = parts[1]
                    pct_str = parts[2]
                    count_num = int(re.search(r'\d+', count_str).group(0))
                    pct_num = float(pct_str.rstrip('%')) / 100.0
                    top_channels.append({
                        "rank": len(top_channels) + 1,
                        "channel_name": ch_name,
                        "track_count": count_num,
                        "track_count_display": count_str,
                        "percentage": pct_str,
                        "percentage_ratio": round(pct_num, 4)
                    })

    # 4. Detailed Tracklist
    tracks = []
    in_tracklist = False
    for line in lines:
        if '## 🎵 完整曲目詳細清單' in line:
            in_tracklist = True
            continue
        if in_tracklist:
            if line.startswith('|') and not line.startswith('| #') and not line.startswith('|---'):
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) == 10:
                    idx = int(parts[0])
                    title = parts[1]
                    channel = parts[2]
                    duration = parts[3]
                    views_fmt = parts[4]
                    views = int(views_fmt.replace(',', ''))
                    likes_fmt = parts[5]
                    likes = int(likes_fmt.replace(',', ''))
                    published_date = parts[6]
                    
                    links_col = parts[7]
                    yt_m = re.search(r'\[YT\]\((https?://[^\)]+)\)', links_col)
                    ytm_m = re.search(r'\[YTM\]\((https?://[^\)]+)\)', links_col)
                    yt_url = yt_m.group(1) if yt_m else ""
                    ytm_url = ytm_m.group(1) if ytm_m else ""
                    
                    video_id = parts[8].strip(' `')
                    playlist_item_id = parts[9].strip(' `')

                    tracks.append({
                        "index": idx,
                        "title": title,
                        "channel": channel,
                        "duration": duration,
                        "views": views,
                        "views_formatted": views_fmt,
                        "likes": likes,
                        "likes_formatted": likes_fmt,
                        "published_date": published_date,
                        "youtube_url": yt_url,
                        "youtube_music_url": ytm_url,
                        "video_id": video_id,
                        "playlist_item_id": playlist_item_id
                    })

    data = {
        "title": doc_title,
        "note": doc_note,
        "overview": overview,
        "top_channels": top_channels,
        "tracks": tracks
    }
    return data

def verify_data(md_path, json_data):
    with open(md_path, 'r', encoding='utf-8') as f:
        md_text = f.read()
    md_lines = md_text.splitlines()

    errors = []

    # Verify overview
    ov = json_data["overview"]
    if ov["playlist_name"] != "skills Test":
        errors.append(f"playlist_name mismatch: {ov['playlist_name']}")
    if ov["playlist_id"] != "PLLpKeZeMXlNY":
        errors.append(f"playlist_id mismatch: {ov['playlist_id']}")
    if ov["playlist_url"] != "https://www.youtube.com/playlist?list=PLLpKeZeMXlNY":
        errors.append(f"playlist_url mismatch: {ov['playlist_url']}")
    if ov["playlist_music_url"] != "https://music.youtube.com/playlist?list=PLLpKeZeMXlNY":
        errors.append(f"playlist_music_url mismatch: {ov['playlist_music_url']}")
    if ov["total_tracks"] != 192:
        errors.append(f"total_tracks mismatch: {ov['total_tracks']}")
    if ov["total_duration"] != "12:45:28":
        errors.append(f"total_duration mismatch: {ov['total_duration']}")
    if ov["total_views"] != 43263726446:
        errors.append(f"total_views mismatch: {ov['total_views']}")
    if ov["total_likes"] != 339078608:
        errors.append(f"total_likes mismatch: {ov['total_likes']}")
    if ov["private_or_invalid_videos"] != 0:
        errors.append(f"private_or_invalid_videos mismatch: {ov['private_or_invalid_videos']}")
    if ov["fetched_at"] != "2026-09-01 19:55:04":
        errors.append(f"fetched_at mismatch: {ov['fetched_at']}")

    # Verify top channels count
    if len(json_data["top_channels"]) != 10:
        errors.append(f"top_channels count mismatch: {len(json_data['top_channels'])} != 10")

    # Verify tracks
    if len(json_data["tracks"]) != 192:
        errors.append(f"tracks count mismatch: {len(json_data['tracks'])} != 192")

    track_lines = [l for l in md_lines[37:] if l.strip().startswith('|') and not l.strip().startswith('| #') and not l.strip().startswith('|---')]
    if len(track_lines) != 192:
        errors.append(f"md track lines count mismatch: {len(track_lines)} != 192")

    for i, (t, l) in enumerate(zip(json_data["tracks"], track_lines)):
        parts = [p.strip() for p in l.split('|')[1:-1]]
        
        # 1. Index
        if t["index"] != int(parts[0]):
            errors.append(f"Track {i+1} index mismatch: {t['index']} != {parts[0]}")
        # 2. Title
        if t["title"] != parts[1]:
            errors.append(f"Track {i+1} title mismatch: {t['title']} != {parts[1]}")
        # 3. Channel
        if t["channel"] != parts[2]:
            errors.append(f"Track {i+1} channel mismatch: {t['channel']} != {parts[2]}")
        # 4. Duration
        if t["duration"] != parts[3]:
            errors.append(f"Track {i+1} duration mismatch: {t['duration']} != {parts[3]}")
        # 5. Views
        if t["views_formatted"] != parts[4] or t["views"] != int(parts[4].replace(',', '')):
            errors.append(f"Track {i+1} views mismatch: {t['views_formatted']} vs {parts[4]}")
        # 6. Likes
        if t["likes_formatted"] != parts[5] or t["likes"] != int(parts[5].replace(',', '')):
            errors.append(f"Track {i+1} likes mismatch: {t['likes_formatted']} vs {parts[5]}")
        # 7. Published date
        if t["published_date"] != parts[6]:
            errors.append(f"Track {i+1} published_date mismatch: {t['published_date']} != {parts[6]}")
        # 8. URLs
        if t["youtube_url"] not in parts[7]:
            errors.append(f"Track {i+1} youtube_url mismatch: {t['youtube_url']} not in {parts[7]}")
        if t["youtube_music_url"] not in parts[7]:
            errors.append(f"Track {i+1} youtube_music_url mismatch: {t['youtube_music_url']} not in {parts[7]}")
        # 9. Video ID
        if t["video_id"] != parts[8].strip(' `'):
            errors.append(f"Track {i+1} video_id mismatch: {t['video_id']} != {parts[8]}")
        # 10. Playlist item ID
        if t["playlist_item_id"] != parts[9].strip(' `'):
            errors.append(f"Track {i+1} playlist_item_id mismatch: {t['playlist_item_id']} != {parts[9]}")

    return errors

if __name__ == '__main__':
    md_file = os.path.join('User Testing', 'UserTest_Task3-1.md')
    json_file = os.path.join('User Testing', 'UserTest_Task3-1.json')

    print(f"Parsing {md_file}...")
    data = parse_markdown(md_file)

    print(f"Writing to {json_file}...")
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("Verifying data integrity between Markdown and JSON...")
    errs = verify_data(md_file, data)
    if errs:
        print(f"VERIFICATION FAILED with {len(errs)} errors:")
        for e in errs[:20]:
            print(f" - {e}")
        sys.exit(1)
    else:
        print("VERIFICATION SUCCESSFUL: All 192 tracks, 10 top channels, and overview metrics match 100% perfectly!")
