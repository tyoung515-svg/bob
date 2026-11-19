"""Advertise the bounded-network contract and installed Builder profile."""
def capabilities(ready, worker):
    installed = ready and worker.get('available') and 'builder' in worker.get('profile_revisions',{})
    return {op:{'version':2,'contract_versions':[2],'implemented':True,
                'available':bool(installed if op=='builder.approve' else ready),
                'reason':None if installed or ready and op!='builder.approve' else 'installed_builder_profile_required',
                'owner_session_required':op in {'builder.mode.set','builder.approve','builder.network.set'},
                'network':{'policy':'lane-proxy/1','connect_method_visibility':False},'target':'bob-lane-workspace'}
            for op in ('builder.mode.get','builder.mode.set','builder.preview','builder.approve','builder.approval.get','builder.approvals.get',
                       'builder.network.set','builder.network_log','builder.task.end','builder.project.result','workspace.browse')}
