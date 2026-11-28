# Image for ClusterFuzzLite: OSS-Fuzz's Python builder with Atheris, pinned by
# digest so the build is reproducible.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:ed78b82bdc5a9fde750bc83f50594042a8918f2ddb5a2f6e7b1a4b8ad79c893c
COPY . $SRC/frappex-client
WORKDIR $SRC/frappex-client
COPY .clusterfuzzlite/build.sh $SRC/
