"""What may still match tests/old_ids.py, by family (the plan's section 6: what
keeps its word) and file. tests/test_no_old_ids.py fails on a match that
isn't here, and on an entry here that no longer matches anything.

A new match is a name missed: rename it. Only what belongs to a family
that keeps its word goes here, under that family.
"""

ALLOWED: dict[str, dict[str, tuple[str, ...]]] = {
    "a batch route's or form's word": {
        'ocr': (
            'src/server/api/routers/assets.py', 'tests/test_assets_submission_api.py', 'tests/test_corrections.py',
            'tests/test_lineage_api.py', 'tests/test_ocr_table.py', 'tests/test_redo_on_change.py',
            'tests/test_scan_content_hash.py', 'tests/test_search_sync_real.py', 'tests/test_vision_submit.py',
        ),
        'vision': (
            'docs/cursor-api.md', 'src/server/api/routers/assets.py', 'src/server/api/routers/ingest.py',
            'tests/derived.py', 'tests/test_assets_submission_api.py', 'tests/test_browse_filters.py',
            'tests/test_facets_filtered.py', 'tests/test_failures_you_can_see.py', 'tests/test_ingest.py',
            'tests/test_last_modified.py', 'tests/test_lineage_api.py', 'tests/test_ocr_table.py',
            'tests/test_producers_run_api.py',
            'tests/test_public_search_cap.py', 'tests/test_reconciler.py', 'tests/test_saved_search_as_project.py',
            'tests/test_scene_redo_api.py', 'tests/test_scheduler_queue.py', 'tests/test_search_cross_library.py',
            'tests/test_search_filters.py', 'tests/test_search_slow.py', 'tests/test_search_sync_real.py',
            'tests/test_unified_query.py', 'tests/test_vision_submit.py',
        ),
    },
    "a call's name (Resolve's plug-in, its own)": {
        'thumbnail': ('clients/resolve/plugin/renderer.js',),
    },
    "a command's name": {
        'video-preview': ('src/client/cli/commands/settings.py', 'tests/test_cli_settings.py',),
    },
    "a downloaded file's name, as before (scripts look for it)": {
        'proxy': ('src/client/cli/main.py',),
        'thumbnail': ('src/client/cli/main.py',),
    },
    "a field's name": {
        'clip': (
            'docs/cursor-api.md', 'src/producers/location/work.py', 'src/server/repository/locations.py',
            'tests/test_location_producer.py',
        ),
        'faces': (
            'clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/FaceClusterTests.swift',
            'clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/FaceListItemTests.swift',
            'docs/cursor-api.md', 'src/processing/ai_worker_client.py', 'src/producers/faces/detect.py',
            'src/producers/video_faces/work.py', 'src/server/api/routers/people.py', 'src/server/api/routers/video.py',
            'src/server/faces/server_face_provider.py',
            'src/server/repository/faces.py', 'tests/ai_machine_server.py', 'tests/faces.py', 'tests/test_ai_worker.py',
            'tests/test_ai_worker_models.py', 'tests/test_assets_submission_api.py',
            'tests/test_face_detection_slow.py', 'tests/test_face_search_process.py', 'tests/test_face_settings.py',
            'tests/test_faces_guard.py',
            'tests/test_library_sight.py', 'tests/test_lineage_api.py', 'tests/test_orphan_asset_children_slow.py',
            'tests/test_people_api_slow.py', 'tests/test_people_clusters_api.py', 'tests/test_retired_endpoints.py',
            'tests/test_scheduler_models.py', 'tests/test_scheduler_runners.py', 'tests/test_server_images.py',
            'tests/test_video_faces.py',
            'tests/test_video_faces_slow.py',
        ),
        'location': (
            'clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/ModelDecodingTests.swift',
            'docs/cursor-api.md', 'src/own_plugins/geonames/geonames.py', 'src/server/repository/clip_inputs.py',
            'src/ui/web/src/pages/settings/ProcessingSection.test.tsx', 'tests/plugins/facts/facts.py', 'tests/test_follow_moves.py', 'tests/test_geonames_plugin_acceptance.py',
            'tests/test_library_admins.py', 'tests/test_location.py', 'tests/test_location_producer.py',
            'tests/test_plugin_work.py', 'tests/test_public_library_middleware.py', 'tests/test_public_no_emails.py',
            'tests/test_retry_after.py', 'tests/test_video_playback_api.py',
        ),
        'ocr': (
            'src/client/cli/main.py', 'src/server/search/sync.py', 'tests/derived.py',
            'tests/test_search_sync_races_slow.py',
        ),
        'preview': ('docs/data-names.md', 'src/producers/proxy/__init__.py',),
        'scenes': (
            'docs/cursor-api.md', 'src/producers/scene_likeness/work.py', 'src/producers/scene_vision/work.py',
            'src/producers/scenes/work.py',
            'src/producers/video_description/work.py', 'tests/scenes.py', 'tests/test_lineage_api.py', 'tests/test_scene_detail.py',
            'tests/test_scene_likeness.py',
            'tests/test_scene_redo_api.py', 'tests/test_scheduler_runners.py', 'tests/test_video_api.py',
            'tests/test_video_faces.py', 'tests/test_video_index.py',
        ),
        'thumbnail': ('docs/data-names.md', 'src/producers/proxy/__init__.py',),
        'transcript': ('tests/test_follow_moves.py',),
    },
    "a filter's type": {
        'location': ('src/server/models/query_filter.py', 'src/ui/web/src/lib/queryFilter.ts',),
    },
    "a job's input (Work.inputs)": {
        'analysis_proxy': ('src/producers/runner.py',),
    },
    "a model's id": {
        'clip': (
            'clients/lumiverb-app/Sources/iOS/iOSBrowseAppContext.swift',
            'clients/lumiverb-app/Sources/macOS/AppState.swift', 'docs/cursor-api.md',
            'src/processing/workers/embeddings/clip_provider.py', 'src/producers/clip/__init__.py',
            'src/server/api/routers/similarity.py', 'src/server/embeddings/clip_provider.py',
            'tests/ai_machine_server.py', 'tests/test_ai_worker.py',
            'tests/test_assets_submission_api.py', 'tests/test_batch_endpoints.py', 'tests/test_bug_fixes.py',
            'tests/test_clip_guard.py', 'tests/test_lineage_api.py', 'tests/test_producer_work.py',
            'tests/test_reconciler.py', 'tests/test_scheduler_runners.py', 'tests/test_similarity_endpoint.py',
            'tests/test_similarity_slow.py',
        ),
        'insightface': (
            'docs/cursor-api.md', 'src/processing/workers/faces/insightface_provider.py',
            'src/producers/video_faces/work.py', 'src/server/api/routers/assets.py', 'src/server/api/routers/video.py',
            'src/server/models/tenant.py', 'tests/test_ai_worker.py', 'tests/test_assets_submission_api.py',
            'tests/test_cluster_cache_slow.py', 'tests/test_face_detection.py', 'tests/test_face_detection_slow.py',
            'tests/test_face_nearest_people_slow.py', 'tests/test_face_redetection_representative_slow.py',
            'tests/test_faces_guard.py', 'tests/test_orphan_asset_children_slow.py',
            'tests/test_preserve_human_data_slow.py', 'tests/test_producer_settings_api.py',
            'tests/test_scheduler_runners.py', 'tests/test_similarity_slow.py',
            'tests/test_trashed_asset_face_filter_slow.py', 'tests/test_video_faces.py',
        ),
    },
    "a package's name": {
        'insightface': ('src/processing/faces_guard.py', 'src/producers/faces/detect.py',),
    },
    "a package's name (its models' folder)": {
        'faces': ('tests/test_models_dir.py',),
        'insightface': (
            'docs/cursor-api.md', 'src/processing/models_dir.py', 'tests/test_face_settings.py',
            'tests/test_insightface_model_root.py', 'tests/test_models_dir.py',
        ),
    },
    "a plug-in's input (protocol 1)": {
        'faces': ('tests/test_plugin_manifest.py',),
        'location': ('docs/writing-a-plugin.md', 
            'docs/cursor-api.md', 'docs/data-names.md', 'src/plugins/manifest.py', 'tests/test_plugin_install.py',
            'tests/test_plugin_manifest.py',
        ),
        'preview': ('docs/writing-a-plugin.md', 
            'docs/cursor-api.md', 'docs/data-names.md', 'src/plugins/manifest.py', 'src/producers/plugin_work.py',
            'tests/test_plugin_install.py', 'tests/test_plugin_manifest.py',
        ),
    },
    'a pool': {
        'probe': (
            'src/producers/pools.py', 'tests/test_processing_pause_api.py', 'tests/test_producer_contract.py',
            'tests/test_scheduler_service.py', 'tests/test_scheduler_status_api.py',
        ),
        'render': (
            'src/producers/pools.py', 'tests/test_processing_eta.py', 'tests/test_scheduler_service.py',
            'tests/test_scheduler_status_api.py',
        ),
        'scenes': ('src/producers/pools.py', 'tests/test_scheduler_service.py',),
        'vision': ('tests/test_scheduler_service.py',),
    },
    "a producer's unit": {
        'clip': (
            'docs/architecture.md', 'docs/cursor-api.md', 'src/producers/__init__.py', 'src/producers/contract.py',
            'src/server/api/routers/producers.py', 'src/ui/web/src/api/client.ts',
            'src/ui/web/src/pages/settings/ProcessingSection.test.tsx',
        ),
    },
    'a program': {
        'exiftool': ('README.md', 'src/server/config.py',),
        'ffprobe': (
            'README.md', 'src/processing/ingest.py', 'src/processing/video/analysis_proxy.py',
            'src/processing/video/audio.py', 'src/processing/video/probe.py',
            'src/processing/video/video_scanner.py', 'src/server/api/routers/playback.py',
            'src/server/uploaded_video.py', 'tests/test_analysis_proxy_render.py',
            'tests/test_browser_playable_video.py', 'tests/test_ingest_dimensions.py',
            'tests/test_producer_settings_client.py', 'tests/test_producer_work.py', 'tests/test_scheduler_runners.py',
            'tests/test_uploaded_video.py', 'tests/test_video_faces_slow.py', 'tests/test_video_input_whitelists.py',
            'tests/test_video_playback_api.py', 'tests/test_video_probe.py', 'tests/test_video_scanner_window.py',
        ),
    },
    "a query's key": {
        'faces': ('src/ui/web/src/components/Lightbox.tsx',),
    },
    'a reserved plug-in id (one installed before part 1, left out: section 8)': {
        'faces': ('tests/test_plugin_install.py',),
    },
    'a retired filter (410)': {
        'missing_faces': ('tests/test_retired_endpoints.py',),
    },
    "a retired route's word (410)": {
        'preview': ('tests/test_artifacts.py',),
        'proxy': ('tests/test_artifacts.py',),
        'thumbnail': ('tests/test_artifacts.py',),
    },
    'a route': {
        'faces': ('tests/test_producer_settings_api.py',),
        'scenes': ('src/producers/video_faces/work.py', 'tests/test_video_faces_slow.py',),
    },
    "a search hit's type": {
        'scenes': ('src/server/api/routers/query.py',),
        'transcript': (
            'clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/ModelDecodingTests.swift',
            'clients/resolve/test/search.test.js', 'docs/cursor-api.md', 'src/server/api/routers/query.py',
            'src/server/search/rank.py', 'src/server/search/text_search.py', 'src/ui/web/src/api/types.ts',
            'src/ui/web/src/lib/foundBy.test.ts',
            'src/ui/web/src/lib/foundBy.ts', 'tests/test_changed_file_new_clip.py',
            'tests/test_public_search_cap.py', 'tests/test_restore_reindex_slow.py', 'tests/test_search_found_by.py',
            'tests/test_search_sync_real.py', 'tests/test_text_search.py', 'tests/test_transcription.py',
        ),
    },
    'a short name (lumiverb. left off)': {
        'preview': (
            'src/client/cli/main.py', 'tests/test_artifacts.py', 'tests/test_cli.py', 'tests/test_cli_pause.py',
        ),
    },
    'a table': {
        'faces': (
            'docs/cursor-api.md', 'src/producers/faces/__init__.py', 'src/producers/video_faces/__init__.py',
            'src/server/models/tenant.py', 'src/server/upgrade/steps/cleanup_orphan_asset_children.py',
            'tests/test_trashed_asset_face_filter_slow.py',
        ),
    },
    "a test's own words": {
        'preview': ('tests/test_video_playback_api.py',),
        'proxy': ('tests/test_video_playback_api.py',),
    },
    "a transcript's source": {
        'whisper': (
            'docs/cursor-api.md', 'src/producers/transcript/work.py',
            'src/ui/web/src/components/TranscriptActions.test.tsx', 'tests/test_ai_machines.py',
            'tests/test_corrections.py', 'tests/test_lineage_api.py', 'tests/test_preserve_human_data_slow.py',
            'tests/test_redo_on_change.py', 'tests/test_scheduler_runners.py', 'tests/test_transcription.py',
        ),
    },
    'a word': {
        'clip': ('src/ui/web/src/pages/AdminAccountsPage.tsx', 
            'clients/resolve/plugin/renderer.js', 'src/client/cli/commands/archive.py',
            'src/client/cli/commands/projects.py', 'src/client/cli/commands/settings.py', 'src/client/cli/main.py',
            'src/server/api/routers/libraries.py', 'src/server/api/routers/projects.py',
            'src/server/api/routers/tenant.py', 'src/server/export/fcp7.py', 'src/server/search/understand.py',
            'src/ui/web/src/lib/useScrollAnchor.ts',
            'src/ui/web/src/lib/waits.ts', 'src/ui/web/src/pages/LibraryLocationSection.tsx',
            'src/ui/web/src/pages/ProjectsPage.tsx', 'src/ui/web/src/pages/settings/ProcessingSection.tsx',
            'tests/test_device_keys.py', 'tests/test_video_faces.py',
        ),
        'location': ('tests/test_geonames_program.py', 'tests/test_ingest_dimensions.py',),
        'preview': ('src/ui/web/src/components/VideoPlayer.test.tsx', 'src/ui/web/src/components/VideoPlayer.tsx',),
        'probe': ('tests/test_geonames_plugin_acceptance.py',),
        'proxy': (
            'clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/BoundedDiskCacheTests.swift',
            'clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/ModelDecodingTests.swift',
            'tests/test_scheduler_service.py',
        ),
        'render': ('tests/test_upgrade_backfill_sha256.py',),
        'scenes': ('src/producers/location/infer.py',),
        'transcript': (
            'src/ui/web/src/components/Lightbox.tsx', 'src/ui/web/src/pages/settings/ProcessingSection.tsx',
        ),
        'video_preview': ('tests/test_artifacts.py',),
    },
    'an AI job': {
        'clip': (
            'docs/cursor-api.md', 'src/processing/ai_worker_client.py', 'src/processing/clip_guard.py',
            'src/producers/clip/work.py', 'src/producers/pools.py', 'src/server/scheduler/models.py',
            'src/ui/web/src/pages/settings/AiSection.test.tsx', 'src/ui/web/src/pages/settings/AiSection.tsx',
            'tests/test_ai_machines.py', 'tests/test_ai_worker.py',
            'tests/test_clip_guard.py', 'tests/test_producers_registry.py', 'tests/test_scheduler_e2e.py',
            'tests/test_scheduler_runners.py', 'tests/test_scheduler_service.py',
        ),
        'faces': (
            'docs/cursor-api.md', 'src/processing/faces_guard.py', 'src/producers/faces/work.py',
            'src/producers/pools.py', 'src/server/scheduler/models.py',
            'src/ui/web/src/pages/settings/AiSection.test.tsx', 'src/ui/web/src/pages/settings/AiSection.tsx',
            'tests/test_ai_machines.py',
            'tests/test_preserve_human_data_slow.py', 'tests/test_producers_registry.py', 'tests/test_scheduler_e2e.py',
            'tests/test_scheduler_service.py',
        ),
        'vision': (
            'docs/cursor-api.md', 'src/processing/vision_guard.py', 'src/producers/pools.py',
            'src/producers/scene_vision/work.py', 'src/producers/video_description/work.py',
            'src/server/api/routers/admin.py', 'src/server/api/routers/ai.py', 'src/server/api/routers/tenant.py',
            'src/server/repository/ai_machines.py', 'src/server/scheduler/account.py', 'src/server/search/assist.py',
            'src/ui/web/src/lib/waits.test.ts', 'src/ui/web/src/pages/settings/AiSection.test.tsx',
            'tests/test_search_assisted.py',
            'src/ui/web/src/pages/settings/AiSection.tsx', 'src/ui/web/src/pages/settings/ProcessingSection.test.tsx',
            'tests/ai_machine_fakes.py', 'tests/test_ai_machines.py', 'tests/test_ai_pool.py', 'tests/test_ai_requests.py',
            'tests/test_ai_try.py',
            'tests/test_cli_ai.py', 'tests/test_corrections.py', 'tests/test_crashes_and_new_versions.py', 'tests/test_no_silent_waits.py',
            'tests/test_processing_eta.py', 'tests/test_processing_pause_api.py', 'tests/test_producer_contract.py',
            'tests/test_producers_registry.py', 'tests/test_redo_on_change.py', 'tests/test_scheduler_account.py',
            'tests/test_scheduler_e2e.py', 'tests/test_scheduler_runners.py', 'tests/test_scheduler_service.py',
            'tests/test_scheduler_status_api.py', 'tests/test_system_health.py', 'tests/test_vision_guard.py',
        ),
    },
    'an answer saved before part 3 (--compare and its test)': {
        'ocr': ('tests/test_cli_producers.py',),
        'plugin_facts': ('tests/test_cli_producers.py',),
        'plugin_geonames': ('tests/test_cli_producers.py',),
        'vision': ('tests/test_cli_producers.py',),
    },
    'an old spelling, refused (its test)': {
        '/producers/faces': ('tests/test_names_taken.py',),
        '/producers/transcript': ('tests/test_names_taken.py',),
        '/producers/vision': ('tests/test_names_taken.py',),
        'analysis_proxy': ('tests/test_data_names.py', 'tests/test_names_taken.py',),
        'clip': ('tests/test_names_taken.py',),
        'faces': ('tests/test_names_taken.py', 'tests/test_scheduler_settings.py',),
        'insightface': ('tests/test_data_names.py',),
        'location': ('tests/test_data_names.py',),
        'ocr': ('tests/test_data_names.py', 'tests/test_names_taken.py',),
        'plugin_facts': ('tests/test_data_names.py',),
        'plugin_geonames': ('tests/test_cli_pause.py', 'tests/test_cli_producers.py', 'tests/test_names_taken.py',),
        'preview': ('tests/test_cli.py', 'tests/test_data_names.py',),
        'probe': ('tests/test_data_names.py', 'tests/test_device_keys.py',),
        'proxy': (
            'tests/test_cli.py', 'tests/test_cli_pause.py', 'tests/test_data_names.py', 'tests/test_device_keys.py',
            'tests/test_names_taken.py',
        ),
        'redo_ocr': ('tests/test_data_names.py',),
        'redo_vision': ('tests/test_data_names.py',),
        'render': ('tests/test_data_names.py',),
        'scene_rep': ('tests/test_data_names.py', 'tests/test_device_keys.py',),
        'scenes': ('tests/test_data_names.py',),
        'thumbnail': ('tests/test_cli.py', 'tests/test_data_names.py', 'tests/test_names_taken.py',),
        'transcript': ('tests/test_names_taken.py',),
        'video_preview': ('tests/test_names_taken.py',),
        'vision': (
            'tests/test_cli_pause.py', 'tests/test_cli_producers.py', 'tests/test_data_names.py',
            'tests/test_device_keys.py', 'tests/test_names_taken.py', 'tests/test_server_pausing.py',
        ),
    },
    "an old upgrade step's label": {
        'proxy': ('src/server/upgrade/steps/backfill_artifact_sha256.py',),
        'thumbnail': ('src/server/upgrade/steps/backfill_artifact_sha256.py',),
    },
    "an older revision's (a migration's test)": {
        'analysis_proxy': ('tests/test_migration_pause_all_row.py', 'tests/test_migration_quality_notch.py',),
        'capture': ('tests/test_migration_pause_switches.py',),
        'clip': (
            'tests/test_migration_built_in_does_clip.py', 'tests/test_migration_lineage.py',
            'tests/test_migration_pause_all_row.py',
        ),
        'faces': (
            'tests/test_migration_built_in_finds_faces.py', 'tests/test_migration_face_levels.py',
            'tests/test_migration_lineage.py', 'tests/test_migration_pause_all_row.py',
            'tests/test_migration_producer_pauses.py', 'tests/test_migrations.py',
        ),
        'insightface': (
            'tests/test_migration_face_levels.py', 'tests/test_migration_lineage.py', 'tests/test_migrations.py',
        ),
        'insightface-video': ('tests/test_migration_face_levels.py',),
        'location': ('tests/test_migration_library_infer_location.py', 'tests/test_migration_pause_switches.py',),
        'ocr': (
            'tests/test_migration_content_hashes_name_their_scheme.py', 'tests/test_migration_lineage.py',
            'tests/test_migration_location_groundwork.py', 'tests/test_migration_pause_all_row.py',
            'tests/test_migration_pause_switches.py', 'tests/test_migration_producer_pauses.py',
        ),
        'plugin_geonames': ('tests/test_migration_geonames_switch_over.py',),
        'probe': ('tests/test_migration_pause_all_row.py',),
        'producer.analysis_proxy': ('tests/test_migration_quality_notch.py',),
        'producer.faces': ('tests/test_migration_face_levels.py',),
        'producer.location': ('tests/test_migration_library_infer_location.py',),
        'proxy': ('tests/test_migration_lineage.py',),
        'scene-vision': ('tests/test_migration_producer_upgrades.py',),
        'scene_vision': ('tests/test_migration_pause_all_row.py', 'tests/test_migration_producer_upgrades.py',),
        'scenes': (
            'tests/test_migration_content_hashes_name_their_scheme.py', 'tests/test_migration_pause_all_row.py',
            'tests/test_migration_scene_index_and_crashes.py',
        ),
        'transcript': ('tests/test_migration_lineage.py', 'tests/test_migration_pause_all_row.py',),
        'video_faces': ('tests/test_migration_face_levels.py', 'tests/test_migration_pause_switches.py',),
        'vision': (
            'tests/test_migration_ai_job_models.py', 'tests/test_migration_ai_machines.py',
            'tests/test_migration_built_in_does_clip.py', 'tests/test_migration_built_in_finds_faces.py',
            'tests/test_migration_geonames_switch_over.py', 'tests/test_migration_library_scans_pause.py',
            'tests/test_migration_lineage.py', 'tests/test_migration_pause_all_row.py',
            'tests/test_migration_pause_switches.py', 'tests/test_migration_processing_paused.py',
            'tests/test_migration_redo_on_change.py', 'tests/test_migration_transcripts_job.py',
        ),
        'whisper': ('tests/test_migration_lineage.py', 'tests/test_migration_machine_transcripts.py',),
    },
    "the dispatcher test's own kinds": {
        'faces': ('tests/test_scheduler_dispatch.py',),
        'ocr': ('tests/test_scheduler_dispatch.py',),
        'probe': ('tests/test_scheduler_dispatch.py',),
        'redo_vision': ('tests/test_scheduler_dispatch.py',),
        'render': ('tests/test_scheduler_dispatch.py',),
        'vision': ('tests/test_scheduler_dispatch.py',),
    },
    "the names' history (WAS, the page's Was, the migrations' mappings, their tests)": {
        'analysis-proxy': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
        'analysis_proxy': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_files_named.py',
            'tests/test_migration_names_stored.py', 'tests/test_named_data_acceptance.py',
        ),
        'capture': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'clip': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'exiftool': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
        'faces': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'ffprobe': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
        'insightface': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
        'insightface-video': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
        'location': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'ocr': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'plugin_': ('tests/old_ids.py',),
        'plugin_facts': ('tests/test_named_data_acceptance.py',),
        'plugin_{': ('tests/old_ids.py',),
        'preview': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
        'previews': ('tests/old_ids.py',),
        'probe': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'producer.analysis_proxy': ('tests/test_migration_names_stored.py',),
        'producer.faces': ('tests/test_migration_names_stored.py',),
        'producer.location': ('tests/test_migration_names_stored.py',),
        'producer.transcript': ('tests/test_migration_names_stored.py',),
        'producer.vision': ('tests/test_migration_names_stored.py',),
        'proxy': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'redo_scene_vision': ('tests/test_migration_names_stored.py',),
        'redo_vision': ('src/producers/names.py', 'tests/test_migration_names_stored.py',),
        'render': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'scene-detect': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
        'scene-vision': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
        'scene_rep': ('docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py',),
        'scene_vision': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'scenes': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'thumbnail': ('docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py',),
        'transcript': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'video-preview': ('tests/old_ids.py',),
        'video_faces': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'video_preview': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_files_named.py',
            'tests/test_migration_names_stored.py', 'tests/test_named_data_acceptance.py',
        ),
        'vision': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
            'tests/test_named_data_acceptance.py',
        ),
        'whisper': (
            'docs/data-names.md', 'src/producers/names.py', 'tests/old_ids.py', 'tests/test_migration_names_stored.py',
        ),
    },
    'the previews folder': {
        'previews': (
            'docs/cursor-api.md', 'src/server/storage/store.py', 'src/shared/previews_location.py',
            'tests/test_drives.py', 'tests/test_library_previews_api.py', 'tests/test_new_folder.py',
            'tests/test_previews_location.py', 'tests/test_server_private.py', 'tests/test_server_run.py',
            'tests/test_storage_api.py',
        ),
    },
    "the stored files' folders": {
        'scenes': ('src/server/storage/store.py', 'tests/test_storage.py',),
    },
    'what an older release did': {
        'plugin_geonames': ('tests/test_geonames_plugin_acceptance.py',),
    },
    'whose an error is': {
        'clip': ('docs/writing-a-plugin.md', 
            'src/own_plugins/geonames/geonames.py', 'src/producers/plugin_work.py', 'src/producers/runner.py',
            'src/server/scheduler/service.py', 'tests/plugins/facts/facts.py', 'tests/test_geonames_program.py',
        ),
    },
}
