# ============================================================================
# UNIFIED OBSERVABILITY INGESTION
# ============================================================================

locals {
  clinic_recall_metrics_table                = "ClinicRecallMetrics_CL"
  clinic_recall_metrics_input_stream         = "Custom-ClinicRecallMetrics"
  clinic_recall_metrics_output_stream        = "Custom-${local.clinic_recall_metrics_table}"
  clinic_recall_metrics_destination          = "clinic-recall-log-analytics"
  clinic_recall_metric_environment_predicate = var.environment_name == "staging" ? "Environment in ('staging', 'dev', 'sandbox')" : "Environment == '${var.environment_name}'"

  clinic_recall_metrics_columns = [
    { name = "TimeGenerated", table_type = "dateTime", stream_type = "datetime" },
    { name = "Environment", table_type = "string", stream_type = "string" },
    { name = "Source", table_type = "string", stream_type = "string" },
    { name = "Suite", table_type = "string", stream_type = "string" },
    { name = "WorkflowName", table_type = "string", stream_type = "string" },
    { name = "MetricName", table_type = "string", stream_type = "string" },
    { name = "MetricValue", table_type = "real", stream_type = "real" },
    { name = "Unit", table_type = "string", stream_type = "string" },
    { name = "Threshold", table_type = "real", stream_type = "real" },
    { name = "Comparator", table_type = "string", stream_type = "string" },
    { name = "Passed", table_type = "boolean", stream_type = "boolean" },
    { name = "Phase", table_type = "int", stream_type = "int" },
    { name = "Arm", table_type = "string", stream_type = "string" },
    { name = "ProgrammeUuid", table_type = "string", stream_type = "string" },
    { name = "GitSha", table_type = "string", stream_type = "string" },
    { name = "ImageTag", table_type = "string", stream_type = "string" },
    { name = "Revision", table_type = "string", stream_type = "string" },
    { name = "ModelVersion", table_type = "string", stream_type = "string" },
    { name = "PromptSha", table_type = "string", stream_type = "string" },
    { name = "ConfigSha", table_type = "string", stream_type = "string" },
    { name = "EvalRunId", table_type = "string", stream_type = "string" },
    { name = "WorkflowRunUrl", table_type = "string", stream_type = "string" },
    { name = "ReasonCode", table_type = "string", stream_type = "string" },
    { name = "EvidenceFingerprint", table_type = "string", stream_type = "string" },
  ]
}

resource "azapi_resource" "clinic_recall_metrics_table" {
  type      = "Microsoft.OperationalInsights/workspaces/tables@2023-09-01"
  name      = local.clinic_recall_metrics_table
  parent_id = azurerm_log_analytics_workspace.main.id

  body = {
    properties = {
      plan                 = "Analytics"
      retentionInDays      = var.monitor_metrics_retention_in_days
      totalRetentionInDays = var.monitor_metrics_total_retention_in_days
      schema = {
        name = local.clinic_recall_metrics_table
        columns = [
          for column in local.clinic_recall_metrics_columns : {
            name = column.name
            type = column.table_type
          }
        ]
      }
    }
  }

  schema_validation_enabled = false
}

resource "azurerm_monitor_data_collection_endpoint" "clinic_recall_metrics" {
  name                          = "dce-${var.name}-${var.environment_name}-${local.resource_token}"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_log_analytics_workspace.main.location
  description                   = "OIDC-authenticated aggregate CI and evaluation metric ingestion."
  kind                          = "Linux"
  public_network_access_enabled = true
  tags                          = local.tags

  lifecycle {
    ignore_changes = [tags["deployed_by"]]
  }
}

resource "azurerm_monitor_data_collection_rule" "clinic_recall_metrics" {
  name                        = "dcr-${var.name}-${var.environment_name}-${local.resource_token}"
  resource_group_name         = azurerm_resource_group.main.name
  location                    = azurerm_log_analytics_workspace.main.location
  data_collection_endpoint_id = azurerm_monitor_data_collection_endpoint.clinic_recall_metrics.id
  description                 = "Routes bounded Clinic Recall metrics to Log Analytics."
  tags                        = local.tags

  destinations {
    log_analytics {
      name                  = local.clinic_recall_metrics_destination
      workspace_resource_id = azurerm_log_analytics_workspace.main.id
    }
  }

  data_flow {
    streams       = [local.clinic_recall_metrics_input_stream]
    destinations  = [local.clinic_recall_metrics_destination]
    transform_kql = "source"
    output_stream = local.clinic_recall_metrics_output_stream
  }

  stream_declaration {
    stream_name = local.clinic_recall_metrics_input_stream

    dynamic "column" {
      for_each = local.clinic_recall_metrics_columns

      content {
        name = column.value.name
        type = column.value.stream_type
      }
    }
  }

  lifecycle {
    ignore_changes = [tags["deployed_by"]]
  }

  depends_on = [azapi_resource.clinic_recall_metrics_table]
}

resource "azurerm_role_assignment" "clinic_recall_metrics_ci_publisher" {
  count = var.monitor_ci_principal_id == null ? 0 : 1

  scope                            = azurerm_monitor_data_collection_rule.clinic_recall_metrics.id
  role_definition_name             = "Monitoring Metrics Publisher"
  principal_id                     = var.monitor_ci_principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_application_insights_workbook" "clinic_recall_unified" {
  name                = uuidv5("dns", "${var.name}-${var.environment_name}-clinic-recall-unified-workbook")
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  display_name        = "Clinic Recall Unified Operations (${var.environment_name})"
  description         = "Runtime, release governance, safety, cost, deployment, and aggregate product outcomes."
  source_id           = lower(azurerm_log_analytics_workspace.main.id)
  category            = "workbook"
  data_json = templatefile("${path.module}/dashboards/unified-workbook.json.tftpl", {
    environment_name             = var.environment_name
    metric_environment_predicate = local.clinic_recall_metric_environment_predicate
    workspace_id                 = lower(azurerm_log_analytics_workspace.main.id)
  })
  tags = {
    for key, value in local.tags : key => value
    if key != "hidden-title"
  }

  lifecycle {
    ignore_changes = [tags["deployed_by"]]
  }
}

# ============================================================================
# ALERTING
# ============================================================================

locals {
  clinic_recall_log_alerts = {
    safety_gate_failure = {
      display_name = "Clinic Recall safety or rights gate failed"
      description  = "A hard clinical-safety, opt-out, authorization, ASSERT, or Red Team metric failed."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        ClinicRecallMetrics_CL
        | where ${local.clinic_recall_metric_environment_predicate} and Passed == false
        | where MetricName in (
            'safe_clinical_boundary',
            'merged_safety_false_negatives',
            'lexical_veto_downgrades',
            'opt_out_misses',
            'unauthorized_actions',
            'severity_3_or_higher_traces',
            'recall_assert_violations',
            'inbound_assert_violations',
            'recall_red_team_asr',
            'inbound_red_team_asr'
          )
      KQL
    }
    voice_latency_breach = {
      display_name = "Clinic Recall voice latency gate breached"
      description  = "A first-audio, semantic, classifier, or barge-clear latency metric exceeded its gate."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        ClinicRecallMetrics_CL
        | where ${local.clinic_recall_metric_environment_predicate} and Passed == false
        | where MetricName in (
            'greeting_first_audio_ms',
            'perceived_voice_latency_p95_ms',
            'classifier_voice_end_to_end_p95_ms',
            'barge_in_speech_start_to_clear_p95_ms',
            'probe.semantic_p95_ms'
          )
      KQL
    }
    runtime_first_audio_breach = {
      display_name = "Clinic Recall runtime first-audio latency breached"
      description  = "Turn-level first-audio p95 reached 3 seconds or the maximum exceeded 4 seconds."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppDependencies
        | where Name startswith 'voice.turn.' and Name endswith '.total'
        | extend FirstAudioMs=todouble(Properties['turn.ttfb_ms'])
        | where isnotnull(FirstAudioMs)
        | summarize FirstAudioP95Ms=percentile(FirstAudioMs, 95), FirstAudioMaxMs=max(FirstAudioMs)
        | where FirstAudioP95Ms >= 3000 or FirstAudioMaxMs > 4000
      KQL
    }
    runtime_barge_clear_breach = {
      display_name = "Clinic Recall runtime barge-clear latency breached"
      description  = "A recorded Speech Cascade barge-in took more than 300 ms to take effect."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppMetrics
        | where Name == 'speech_cascade.barge_in.latency'
        | summarize BargeClearMaxMs=max(Max)
        | where BargeClearMaxMs > 300
      KQL
    }
    warmup_failure = {
      display_name = "Clinic Recall VoiceLive warm-up failed"
      description  = "The latest deferred warm-up did not report a successful warmed token state."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT30M"
      threshold    = 0
      query        = <<-KQL
        AppMetrics
        | where Name == 'startup.warmup.completed'
        | summarize arg_max(TimeGenerated, *) by AppRoleInstance
        | where tostring(Properties['warmup.success']) != 'true' or tostring(Properties['warmup.status']) != 'warmed'
      KQL
    }
    warmup_token_count = {
      display_name = "Clinic Recall VoiceLive warm-up token count invalid"
      description  = "The latest VoiceLive warm-up did not make exactly one token request."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT30M"
      threshold    = 0
      query        = <<-KQL
        AppMetrics
        | where Name == 'startup.warmup.token_request_count'
        | summarize arg_max(TimeGenerated, *) by AppRoleInstance
        | where Sum != 1
      KQL
    }
    governance_stale = {
      display_name = "Clinic Recall governance evidence is stale"
      description  = "No AgentOps, ASSERT, Red Team, or Doctor evidence was ingested within 26 hours."
      severity     = 1
      frequency    = "PT1H"
      window       = "P2D"
      threshold    = 0
      query        = <<-KQL
        ClinicRecallMetrics_CL
        | where ${local.clinic_recall_metric_environment_predicate} and Source in ('agentops', 'assert', 'red_team', 'doctor')
        | summarize LastEvidence=max(TimeGenerated)
        | where isnull(LastEvidence) or LastEvidence < ago(26h)
      KQL
    }
    doctor_blocker = {
      display_name = "Clinic Recall Doctor reported a blocker"
      description  = "The latest release Doctor evidence contains one or more blockers."
      severity     = 1
      frequency    = "PT15M"
      window       = "PT1H"
      threshold    = 0
      query        = <<-KQL
        ClinicRecallMetrics_CL
        | where ${local.clinic_recall_metric_environment_predicate}
        | where MetricName == 'doctor.blocker_count' and MetricValue > 0
      KQL
    }
    readiness_failure = {
      display_name = "Clinic Recall readiness requests are failing"
      description  = "The backend readiness endpoint returned a failed request."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppRequests
        | where Url endswith '/ready' or Name endswith '/ready'
        | where Success == false
      KQL
    }
    exception_burst = {
      display_name = "Clinic Recall runtime exception burst"
      description  = "More than four application exceptions occurred in the evaluation window."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 4
      query        = <<-KQL
        AppExceptions
      KQL
    }
    container_restart = {
      display_name = "Clinic Recall container restart detected"
      description  = "Container Apps emitted restart, termination, or back-off system logs."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        ContainerAppSystemLogs_CL
        | where Log_s has_any ('Back-off restarting failed container', 'Container terminated', 'Killing container')
      KQL
    }
    handoff_sla_breach = {
      display_name = "Clinic Recall critical or high handoff SLA breached"
      description  = "An unacknowledged critical or high handoff crossed its immutable SLA."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), HandoffSeverity=tostring(Properties['severity']), AggregateCount=toint(Properties['count'])
        | where EventName == 'handoff.sla.breach' and HandoffSeverity in ('critical', 'high') and AggregateCount > 0
      KQL
    }
    handoff_destination_unavailable = {
      display_name = "Clinic Recall handoff destination unavailable"
      description  = "The approved primary handoff destination could not be resolved or definitively rejected delivery."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), Outcome=tostring(Properties['outcome']), AggregateCount=toint(Properties['count'])
        | where EventName == 'handoff.notification.outcome' and Outcome == 'destination_unavailable' and AggregateCount > 0
      KQL
    }
    handoff_notification_ambiguity = {
      display_name = "Clinic Recall handoff notification needs reconciliation"
      description  = "An operational notification has an ambiguous provider outcome and will not be replayed."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), Outcome=tostring(Properties['outcome']), AggregateCount=toint(Properties['count'])
        | where EventName == 'handoff.notification.outcome' and Outcome == 'reconcile_required' and AggregateCount > 0
      KQL
    }
    handoff_alternate_page_requested = {
      display_name = "Clinic Recall alternate handoff page requested"
      description  = "A deterministic handoff condition requested the approved on-call action group path."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | extend EventName=tostring(Properties['microsoft.custom_event.name'])
        | where EventName == 'handoff.alternate.requested'
      KQL
    }
    handoff_programme_pause = {
      display_name = "Clinic Recall programme paused for handoff safety"
      description  = "PR-13 pause was requested by a handoff destination, SLA, or owner invariant."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), PauseOutcome=tostring(Properties['outcome'])
        | where EventName == 'handoff.programme.pause' and PauseOutcome == 'paused'
      KQL
    }
    effect_ambiguity_backlog = {
      display_name = "Clinic Recall ambiguous external effects awaiting reconciliation"
      description  = "One or more external effects are parked in reconcile_required and will not be replayed automatically."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), Worker=tostring(Properties['worker']), Outcome=tostring(Properties['outcome']), AggregateCount=toint(Properties['count'])
        | where EventName == 'worker.cycle.summary' and Worker in ('sms_dispatch', 'call_dispatch', 'recording_dispatch', 'rights_dispatch', 'rights_reconcile', 'cliniko_dispatch', 'cliniko_reconcile') and Outcome in ('reconcile_required', 'unresolved', 'exhausted') and AggregateCount > 0
      KQL
    }
    effect_dead_letter = {
      display_name = "Clinic Recall dead-lettered external effects"
      description  = "An external effect exhausted its bounded retries and is dead-lettered with an automatic handoff."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), Worker=tostring(Properties['worker']), Outcome=tostring(Properties['outcome']), AggregateCount=toint(Properties['count'])
        | where EventName == 'worker.cycle.summary' and Worker in ('sms_dispatch', 'cliniko_dispatch') and Outcome == 'dead_lettered' and AggregateCount > 0
      KQL
    }
    callback_ambiguity = {
      display_name = "Clinic Recall provider callbacks need reconciliation"
      description  = "One or more provider callback receipts are parked in reconcile_required."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), Worker=tostring(Properties['worker']), Outcome=tostring(Properties['outcome']), AggregateCount=toint(Properties['count'])
        | where EventName == 'worker.cycle.summary' and Worker == 'callback_reconcile' and Outcome == 'conflicts' and AggregateCount > 0
      KQL
    }
    callback_processing_lag = {
      display_name = "Clinic Recall provider callback processing is lagging"
      description  = "The oldest pending provider callback is older than three 5-minute reconciliation leases."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), CallbackState=tostring(Properties['state']), OldestAgeBucket=tostring(Properties['oldest_age_bucket']), AggregateCount=toint(Properties['count'])
        | where EventName == 'callbacks.queue.snapshot' and CallbackState in ('pending', 'processing') and OldestAgeBucket in ('15m_to_1h', '1h_to_4h', 'over_4h') and AggregateCount > 0
      KQL
    }
    cliniko_readback_conflict = {
      display_name = "Clinic Recall Cliniko write/read-back conflict quarantined"
      description  = "A booking write-back diverged from Cliniko read-back evidence and is quarantined in conflict state."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), Worker=tostring(Properties['worker']), Outcome=tostring(Properties['outcome']), AggregateCount=toint(Properties['count'])
        | where EventName == 'worker.cycle.summary' and Worker in ('cliniko_dispatch', 'cliniko_reconcile') and Outcome == 'conflicts' and AggregateCount > 0
      KQL
    }
    booking_confirmation_grounding = {
      display_name = "Clinic Recall booking confirmation blocked on grounding"
      description  = "A confirmation effect was canceled because verified provider booking authority was absent."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), ReasonCode=tostring(Properties['reason_code']), AggregateCount=toint(Properties['count'])
        | where EventName == 'booking.confirmation.blocked' and ReasonCode == 'booking_confirmation_authority_invalid' and AggregateCount > 0
      KQL
    }
    recording_consent_mismatch = {
      display_name = "Clinic Recall recording consent/provider-state mismatch"
      description  = "Recording provider state conflicts with the deterministic consent ledger."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), ReasonCode=tostring(Properties['reason_code']), AggregateCount=toint(Properties['count'])
        | where EventName == 'recording.consent.mismatch' and ReasonCode in ('provider_outcome_conflict', 'recording_status_reconcile_required') and AggregateCount > 0
      KQL
    }
    rights_deletion_overdue = {
      display_name = "Clinic Recall rights or deletion work is overdue"
      description  = "A rights request, deletion target, or residual approval crossed its due deadline."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), AggregateCount=toint(Properties['count'])
        | where EventName == 'rights.deletion.overdue' and AggregateCount > 0
      KQL
    }
    pilot_cohort_invariant_violation = {
      display_name = "Clinic Recall pilot cohort invariant violated"
      description  = "A cohort, wave, release, or programme-state invariant was breached and failed closed."
      severity     = 0
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), AggregateCount=toint(Properties['count'])
        | where EventName == 'pilot.invariant.violation' and AggregateCount > 0
      KQL
    }
    pilot_configuration_stale = {
      display_name = "Clinic Recall pilot configuration stale or missing"
      description  = "The PR-13 operational configuration snapshot is stale, missing, or lacks release identity; outreach fails closed."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), Reason=tostring(Properties['reason']), AggregateCount=toint(Properties['count'])
        | where EventName == 'pilot.configuration.status' and Reason != 'fresh' and AggregateCount > 0
      KQL
    }
    pilot_release_mismatch = {
      display_name = "Clinic Recall programme release/environment mismatch"
      description  = "An operationally bound pilot programme disagrees with the runtime release or environment identity."
      severity     = 1
      frequency    = "PT5M"
      window       = "PT15M"
      threshold    = 0
      query        = <<-KQL
        AppTraces
        | where TimeGenerated > ago(15m)
        | extend EventName=tostring(Properties['microsoft.custom_event.name']), AggregateCount=toint(Properties['count'])
        | where EventName == 'pilot.release.mismatch' and AggregateCount > 0
      KQL
    }
  }

  clinic_recall_budget_alerts = merge(
    var.monitor_daily_token_budget == null ? {} : {
      daily_token_budget = {
        display_name = "Clinic Recall daily token budget exceeded"
        description  = "Combined input and output token usage exceeded the configured daily budget."
        threshold    = var.monitor_daily_token_budget
        query        = <<-KQL
          AppMetrics
          | where Name in ('gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens')
          | summarize Total=sum(Sum)
        KQL
      }
    },
    var.monitor_daily_estimated_cost_usd == null ? {} : {
      daily_estimated_cost_budget = {
        display_name = "Clinic Recall daily estimated AI cost budget exceeded"
        description  = "The configured token-rate estimate exceeded the daily USD budget."
        threshold    = var.monitor_daily_estimated_cost_usd
        query        = <<-KQL
          AppMetrics
          | where Name == 'gen_ai.usage.estimated_cost_usd'
          | summarize Total=sum(Sum)
        KQL
      }
    }
  )
}

resource "azurerm_monitor_action_group" "clinic_recall" {
  count = length(var.monitor_alert_email_receivers) == 0 ? 0 : 1

  name                = "ag-${var.name}-${var.environment_name}-${local.resource_token}"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = substr("cr-${var.environment_name}", 0, 12)
  tags                = local.tags

  dynamic "email_receiver" {
    for_each = {
      for receiver in var.monitor_alert_email_receivers : receiver.name => receiver
    }

    content {
      name                    = email_receiver.value.name
      email_address           = email_receiver.value.email_address
      use_common_alert_schema = true
    }
  }

  lifecycle {
    ignore_changes = [tags["deployed_by"]]
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "clinic_recall" {
  for_each = local.clinic_recall_log_alerts

  name                    = "alert-${var.name}-${var.environment_name}-${replace(each.key, "_", "-")}"
  resource_group_name     = azurerm_resource_group.main.name
  location                = azurerm_resource_group.main.location
  display_name            = each.value.display_name
  description             = each.value.description
  scopes                  = [azurerm_log_analytics_workspace.main.id]
  severity                = each.value.severity
  evaluation_frequency    = each.value.frequency
  window_duration         = each.value.window
  enabled                 = true
  auto_mitigation_enabled = true
  tags                    = local.tags

  criteria {
    query                   = each.value.query
    time_aggregation_method = "Count"
    threshold               = each.value.threshold
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  dynamic "action" {
    for_each = length(azurerm_monitor_action_group.clinic_recall) == 0 ? [] : [1]

    content {
      action_groups = [azurerm_monitor_action_group.clinic_recall[0].id]
    }
  }

  lifecycle {
    ignore_changes = [tags["deployed_by"]]
  }

  depends_on = [azapi_resource.clinic_recall_metrics_table]
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "clinic_recall_budget" {
  for_each = local.clinic_recall_budget_alerts

  name                    = "alert-${var.name}-${var.environment_name}-${replace(each.key, "_", "-")}"
  resource_group_name     = azurerm_resource_group.main.name
  location                = azurerm_resource_group.main.location
  display_name            = each.value.display_name
  description             = each.value.description
  scopes                  = [azurerm_log_analytics_workspace.main.id]
  severity                = 2
  evaluation_frequency    = "PT1H"
  window_duration         = "P1D"
  enabled                 = true
  auto_mitigation_enabled = true
  tags                    = local.tags

  criteria {
    query                   = each.value.query
    metric_measure_column   = "Total"
    time_aggregation_method = "Maximum"
    threshold               = each.value.threshold
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  dynamic "action" {
    for_each = length(azurerm_monitor_action_group.clinic_recall) == 0 ? [] : [1]

    content {
      action_groups = [azurerm_monitor_action_group.clinic_recall[0].id]
    }
  }

  lifecycle {
    ignore_changes = [tags["deployed_by"]]
  }
}