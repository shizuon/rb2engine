/* Independent libavcodec client. Reproduce the reader's initial measurement,
   flush and rewind. AAC's PNS random state survives a flush: skipping this
   lifecycle changes the decoded high-frequency noise throughout the song. */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <limits.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#include <wchar.h>
#endif
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/samplefmt.h>

static void fail(const char *message) { fprintf(stderr,"AAC reader: %s\n",message);exit(2); }
#ifdef _WIN32
static FILE *utf8_fopen(const char *path,const char *mode) {
    int n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path,-1,NULL,0);
    if(!n)fail("invalid UTF-8 output path");
    wchar_t *wide=malloc((size_t)n*sizeof(wchar_t));
    if(!wide)fail("allocation failed");
    MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path,-1,wide,n);
    FILE *file=_wfopen(wide,strcmp(mode,"wb")==0 ? L"wb" : L"w");
    free(wide);return file;
}
#define fopen utf8_fopen
#define main reader_main
#endif
static int channels(AVFrame *f) {
#if LIBAVUTIL_VERSION_MAJOR >= 57
    return f->ch_layout.nb_channels;
#else
    return f->channels;
#endif
}
static int64_t emit(AVFrame *f,FILE *out) {
    int ch=channels(f),n=f->nb_samples;
    if(ch<1||ch>2||n<=0)fail("unsupported frame shape");
    float *buffer=malloc((size_t)n*ch*sizeof(float));
    if(!buffer)fail("allocation failed");
    if(f->format==AV_SAMPLE_FMT_FLTP) {
        for(int i=0;i<n;i++)for(int c=0;c<ch;c++)
            buffer[i*ch+c]=((float *)f->extended_data[c])[i];
    } else if(f->format==AV_SAMPLE_FMT_FLT) {
        memcpy(buffer,f->data[0],(size_t)n*ch*sizeof(float));
    } else fail("AAC decoder did not return float PCM");
    if(fwrite(buffer,sizeof(float),(size_t)n*ch,out)!=(size_t)n*ch)fail("PCM write failed");
    free(buffer);return n;
}

/* Newer MOV demuxers attach end-discard metadata that the reference reader's
   generation did not. Keep priming skip, decode the tail, then let the reader
   clock cap it to the declared stream length. */
static int send_packet(AVCodecContext *ctx,AVPacket *packet) {
    for(int i=0;i<packet->side_data_elems;i++)
        if(packet->side_data[i].type==AV_PKT_DATA_SKIP_SAMPLES && packet->side_data[i].size>=10)
            memset(packet->side_data[i].data+4,0,4);
    return avcodec_send_packet(ctx,packet);
}
static void record_frame(AVFrame *f,FILE *report,int *count) {
    if(f->pkt_dts==AV_NOPTS_VALUE)fail("frame has no packet timestamp");
    fprintf(report,"%s{\"pkt_dts\":%lld,\"nb_samples\":%d}",
            (*count)++ ? "," : "",(long long)f->pkt_dts,f->nb_samples);
}

int main(int argc,char **argv) {
    if(argc!=4)fail("usage: aac_decode input output.f32 report.json");
    AVFormatContext *fc=NULL;
    if(avformat_open_input(&fc,argv[1],NULL,NULL)<0||avformat_find_stream_info(fc,NULL)<0)
        fail("cannot read input");
    /* Match ffprobe's a:0 and the generic decoder's -map 0:a:0. Selecting
       the default/best stream can pair a later track's PCM with the first
       track's sample rate and timestamps. Single-audio-stream files retain
       the same selection and decoder lifecycle. */
    int ix=-1;
    for(unsigned int i=0;i<fc->nb_streams;i++)
        if(fc->streams[i]->codecpar->codec_type==AVMEDIA_TYPE_AUDIO){ix=(int)i;break;}
    if(ix<0||fc->streams[ix]->codecpar->codec_id!=AV_CODEC_ID_AAC)fail("expected AAC");
    const AVCodec *codec=avcodec_find_decoder(AV_CODEC_ID_AAC);
    AVCodecContext *ctx=avcodec_alloc_context3(codec);
    if(!ctx||avcodec_parameters_to_context(ctx,fc->streams[ix]->codecpar)<0)
        fail("cannot configure AAC decoder");
    /* Needed for libavcodec to advance timestamps after a partial priming skip. */
    ctx->pkt_timebase=fc->streams[ix]->time_base;
    if(avcodec_open2(ctx,codec,NULL)<0)fail("cannot open AAC decoder");
    AVPacket *packet=av_packet_alloc();AVFrame *frame=av_frame_alloc();
    if(!packet||!frame)fail("allocation failed");
    if(avformat_seek_file(fc,ix,INT64_MIN,-10000,INT64_MAX,9)<0)fail("measurement seek failed");
    int warm_frames=0,warm_packets=0;
    int64_t first_pts=AV_NOPTS_VALUE,measured_pts=AV_NOPTS_VALUE;
    for(int attempt=0;attempt<99&&!warm_frames;attempt++) {
        if(av_read_frame(fc,packet)<0)fail("no decoded measurement frame");
        if(packet->stream_index==ix) {
            if(!warm_packets)first_pts=packet->pts;
            warm_packets++;
            if(send_packet(ctx,packet)<0)fail("measurement packet failed");
            int ret;
            while((ret=avcodec_receive_frame(ctx,frame))>=0) {
                if(!warm_frames)measured_pts=frame->pkt_dts;
                warm_frames++;
                av_frame_unref(frame);
            }
            if(ret!=AVERROR(EAGAIN)&&ret!=AVERROR_EOF)fail("measurement decode failed");
        }
        av_packet_unref(packet);
    }
    if(!warm_frames||first_pts==AV_NOPTS_VALUE||measured_pts==AV_NOPTS_VALUE)fail("missing measurement timing");
    avcodec_flush_buffers(ctx);
    if(avformat_seek_file(fc,ix,INT64_MIN,first_pts,INT64_MAX,12)<0)fail("rewind failed");
    avcodec_flush_buffers(ctx);
    FILE *out=fopen(argv[2],"wb");if(!out)fail("cannot open output");
    FILE *report=fopen(argv[3],"w");if(!report)fail("cannot open report");
    fprintf(report,"{\"frames\":[");int frame_count=0;
    int64_t total=0;int ret;
    while((ret=av_read_frame(fc,packet))>=0) {
        if(packet->stream_index==ix) {
            if(send_packet(ctx,packet)<0)fail("packet decode failed");
            int received;
            while((received=avcodec_receive_frame(ctx,frame))>=0) {
                record_frame(frame,report,&frame_count);
                total+=emit(frame,out);av_frame_unref(frame);
            }
            if(received!=AVERROR(EAGAIN)&&received!=AVERROR_EOF)fail("frame decode failed");
        }
        av_packet_unref(packet);
    }
    if(ret!=AVERROR_EOF)fail("input read failed");
    if(avcodec_send_packet(ctx,NULL)<0)fail("decoder drain failed");
    while((ret=avcodec_receive_frame(ctx,frame))>=0){
        record_frame(frame,report,&frame_count);
        total+=emit(frame,out);av_frame_unref(frame);
    }
    if(ret!=AVERROR_EOF)fail("incomplete decoder drain");
    if(fclose(out))fail("cannot finish PCM output");
    fprintf(report,"],\"policy\":\"measure_first_frame_flush_rewind_keep_tail\","
            "\"libavcodec_version\":%u,\"warmup_packets\":%d,\"warmup_frames\":%d,"
            "\"first_packet_pts\":%lld,\"measurement_frame_dts\":%lld,\"decoded_frames\":%lld}\n",
            avcodec_version(),warm_packets,warm_frames,(long long)first_pts,
            (long long)measured_pts,(long long)total);
    if(fclose(report))fail("cannot finish report");
    av_frame_free(&frame);av_packet_free(&packet);avcodec_free_context(&ctx);avformat_close_input(&fc);
    return 0;
}
#ifdef _WIN32
#undef main
int wmain(int argc,wchar_t **wide) {
    char **argv=calloc((size_t)argc+1,sizeof(char *));
    if(!argv)fail("allocation failed");
    for(int i=0;i<argc;i++) {
        int n=WideCharToMultiByte(CP_UTF8,0,wide[i],-1,NULL,0,NULL,NULL);
        argv[i]=malloc((size_t)n);
        if(!n||!argv[i])fail("argument conversion failed");
        WideCharToMultiByte(CP_UTF8,0,wide[i],-1,argv[i],n,NULL,NULL);
    }
    int result=reader_main(argc,argv);
    for(int i=0;i<argc;i++)free(argv[i]);
    free(argv);return result;
}
#endif
