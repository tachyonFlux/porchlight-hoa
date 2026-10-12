from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.core.files.uploadhandler import FileUploadHandler


class RequestSizeLimitHandler(FileUploadHandler):
    """Reject oversized multipart requests before any file data is written to disk.

    Django's DATA_UPLOAD_MAX_MEMORY_SIZE doesn't cover file parts, so without this a large
    POST would be spooled to a temp file before any view (or permission check) runs.
    Put a matching body-size limit on the reverse proxy too.
    """

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        if content_length and content_length > settings.MAX_REQUEST_MB * 1024 * 1024:
            raise RequestDataTooBig("Upload too large.")

    def receive_data_chunk(self, raw_data, start):
        return raw_data

    def file_complete(self, file_size):
        return None
