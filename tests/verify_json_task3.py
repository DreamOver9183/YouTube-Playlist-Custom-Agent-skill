import os
import json
import re
import sys

def run_deep_verification():
    md_path = os.path.join('User Testing', 'UserTest_Task3-1.md')
    json_path = os.path.join('User Testing', 'UserTest_Task3-1.json')

    assert os.path.exists(md_path), f"Markdown file not found: {md_path}"
    assert os.path.exists(json_path), f"JSON file not found: {json_path}"

    with open(md_path, 'r', encoding='utf-8') as f:
        md_lines = f.read().splitlines()

    with open(json_path, 'r', encoding='utf-8') as f:
        json_data = json.load(f)

    print(f"[1/4] Checking Top-Level Keys and Metadata...")
    expected_top_keys = {"title", "note", "overview", "top_channels", "tracks"}
    assert set(json_data.keys()) == expected_top_keys, f"Top level keys mismatch: {set(json_data.keys())}"
    assert "YouTube 播放清單詳細資料清冊" in json_data["title"]
    print("      Top-level metadata verified.")

    print(f"[2/4] Checking Overview Fields...")
    ov = json_data["overview"]
    assert ov["playlist_name"] == "skills Test"
    assert ov["playlist_id"] == "PLLpKeZeMXlNY"
    assert ov["playlist_url"] == "https://www.youtube.com/playlist?list=PLLpKeZeMXlNY"
    assert ov["playlist_music_url"] == "https://music.youtube.com/playlist?list=PLLpKeZeMXlNY"
    assert ov["total_tracks"] == 192
    assert ov["total_duration"] == "12:45:28"
    assert ov["total_views"] == 43263726446
    assert ov["total_likes"] == 339078608
    assert ov["private_or_invalid_videos"] == 0
    assert ov["fetched_at"] == "2026-09-01 19:55:04"
    print("      Overview fields verified.")

    print(f"[3/4] Checking Top 10 Channels...")
    top_channels = json_data["top_channels"]
    assert len(top_channels) == 10, f"Expected 10 channels, got {len(top_channels)}"
    expected_top_names = [
        "ONE OK ROCK - Topic",
        "Lorien Testard - Topic",
        "SMTOWN",
        "Alan Walker",
        "K-391 - Topic",
        "Alan Walker - Topic",
        "BIGBANG",
        "Hiroyuki SAWANO - Topic",
        "JYP Entertainment",
        "Avicii - Topic"
    ]
    expected_counts = [15, 11, 11, 8, 7, 7, 6, 5, 5, 4]
    for i, (ch, exp_name, exp_cnt) in enumerate(zip(top_channels, expected_top_names, expected_counts)):
        assert ch["rank"] == i + 1
        assert ch["channel_name"] == exp_name, f"Channel {i+1} name mismatch: {ch['channel_name']} != {exp_name}"
        assert ch["track_count"] == exp_cnt, f"Channel {i+1} count mismatch: {ch['track_count']} != {exp_cnt}"
    print("      Top 10 Channels verified.")

    print(f"[4/4] Checking All 192 Tracks (10 columns per track)...")
    tracks = json_data["tracks"]
    assert len(tracks) == 192, f"Expected 192 tracks, got {len(tracks)}"

    # Extract table lines from Markdown
    md_track_lines = [l for l in md_lines if re.match(r'\|\s*\d+\s*\|', l)]
    assert len(md_track_lines) == 192, f"Expected 192 table lines in markdown, got {len(md_track_lines)}"

    total_calculated_views = sum(t["views"] for t in tracks)
    total_calculated_likes = sum(t["likes"] for t in tracks)

    for i, (t, md_line) in enumerate(zip(tracks, md_track_lines)):
        cols = [c.strip() for c in md_line.split('|')[1:-1]]
        assert len(cols) == 10, f"Row {i+1} column count error: {cols}"
        
        # 1. Index
        assert t["index"] == int(cols[0]) == (i + 1), f"Row {i+1} index mismatch"
        # 2. Title
        assert t["title"] == cols[1], f"Row {i+1} title mismatch"
        # 3. Channel
        assert t["channel"] == cols[2], f"Row {i+1} channel mismatch"
        # 4. Duration
        assert t["duration"] == cols[3], f"Row {i+1} duration mismatch"
        # 5. Views
        assert t["views_formatted"] == cols[4], f"Row {i+1} views formatted mismatch"
        assert t["views"] == int(cols[4].replace(',', '')), f"Row {i+1} views integer mismatch"
        # 6. Likes
        assert t["likes_formatted"] == cols[5], f"Row {i+1} likes formatted mismatch"
        assert t["likes"] == int(cols[5].replace(',', '')), f"Row {i+1} likes integer mismatch"
        # 7. Published Date
        assert t["published_date"] == cols[6], f"Row {i+1} published date mismatch"
        # 8. Links
        assert f"watch?v={t['video_id']}" in t["youtube_url"], f"Row {i+1} youtube_url mismatch"
        assert f"watch?v={t['video_id']}" in t["youtube_music_url"], f"Row {i+1} youtube_music_url mismatch"
        assert t["youtube_url"] in cols[7], f"Row {i+1} youtube_url in table mismatch"
        assert t["youtube_music_url"] in cols[7], f"Row {i+1} youtube_music_url in table mismatch"
        # 9. Video ID
        assert t["video_id"] == cols[8].strip(' `'), f"Row {i+1} video ID mismatch"
        # 10. Playlist Item ID
        assert t["playlist_item_id"] == cols[9].strip(' `'), f"Row {i+1} playlist item ID mismatch"

    print(f"      Calculated Total Views from tracks: {total_calculated_views:,} (matches overview: {ov['total_views']:,})")
    assert total_calculated_views == ov["total_views"], "Sum of views does not equal overview total_views"
    
    print(f"      Calculated Total Likes from tracks: {total_calculated_likes:,} (matches overview: {ov['total_likes']:,})")
    assert total_calculated_likes == ov["total_likes"], "Sum of likes does not equal overview total_likes"

    print("\n========================================================")
    print(" ALL 192 TRACKS AND ALL METRICS PASSED DEEP VERIFICATION ")
    print("========================================================")

if __name__ == '__main__':
    run_deep_verification()
