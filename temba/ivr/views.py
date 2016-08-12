from __future__ import unicode_literals

import json

from django.core.exceptions import ValidationError
from django.core.files.temp import NamedTemporaryFile
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from temba.channels.models import TWILIO, NEXMO
from temba.channels.models import VERBOICE
from temba.utils import build_json_response
from temba.flows.models import Flow, FlowRun
from .models import IVRCall, IN_PROGRESS, COMPLETED, RINGING


class CallHandler(View):

    @csrf_exempt
    def dispatch(self, *args, **kwargs):
        return super(CallHandler, self).dispatch(*args, **kwargs)

    def get(self, request, *args, **kwargs):
        return HttpResponse("ILLEGAL METHOD")

    def post(self, request, *args, **kwargs):
        call = IVRCall.objects.filter(pk=kwargs['pk']).first()

        if not call:
            return HttpResponse("Not found", status=404)

        channel = call.channel
        channel_type = channel.channel_type
        client = channel.get_ivr_client()

        if channel_type in [TWILIO, VERBOICE] and request.REQUEST.get('hangup', 0):
            if not request.user.is_anonymous():
                user_org = request.user.get_org()
                if user_org and user_org.pk == call.org.pk:
                    client.calls.hangup(call.external_id)
                    return HttpResponse(json.dumps(dict(status='Canceled')), content_type="application/json")
                else:
                    return HttpResponse("Not found", status=404)

        if client.validate(request):
            status = None
            duration = None
            if channel_type in [TWILIO, VERBOICE]:
                status = request.POST.get('CallStatus', None)
                duration = request.POST.get('CallDuration', None)
            elif channel_type in [NEXMO]:
                status = request.POST.get('status')
                duration = request.POST.get('call-duration')

            call.update_status(status, duration, channel_type)

            # update any calls we have spawned with the same
            for child in call.child_calls.all():
                child.update_status(status, duration, channel_type)
                child.save()

            call.save()

            user_response = request.POST.copy()

            hangup = False

            if channel_type in [TWILIO, VERBOICE]:
                # figure out if this is a callback due to an empty gather
                is_empty = '1' == request.GET.get('empty', '0')

                # if the user pressed pound, then record no digits as the input
                if is_empty:
                    user_response['Digits'] = ''

                hangup = 'hangup' == user_response.get('Digits', None)

            elif channel_type in [NEXMO]:
                file_content = request.POST.get('UserRecording', None)

                if file_content is not None:
                    temp = NamedTemporaryFile(delete=True)
                    temp.write(file_content)
                    temp.flush()

            if call.status in [IN_PROGRESS, RINGING] or hangup:
                if call.is_flow():
                    response = Flow.handle_call(call, user_response, hangup=hangup)
                    return HttpResponse(unicode(response))
            else:
                if call.status == COMPLETED:
                    # if our call is completed, hangup
                    run = FlowRun.objects.filter(call=call).first()
                    if run:
                        run.set_completed()
                return build_json_response(dict(message="Updated call status"))

        else:  # pragma: no cover
            # raise an exception that things weren't properly signed
            raise ValidationError("Invalid request signature")

        return build_json_response(dict(message="Unhandled"))
